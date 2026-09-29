"""Queue, backoff, reclaim, STOP and budget."""
import fcntl
import json
import os
import subprocess
import sys

import pytest

from loop import evidence as ev
from loop import execution, harvest
from loop import queue as q
from tests.conftest import fake_packet, make_run


def test_workers_steal_distinct_items(home):
    run_dir = make_run(["login", "cart"])
    a, b = q.claim(run_dir, "w1"), q.claim(run_dir, "w2")
    assert {a["item"], b["item"]} == {"login", "cart"}
    assert q.claim(run_dir, "w3") is None


def test_finish_refuses_a_token_that_no_longer_owns_the_item(home):
    run_dir = make_run(["login"])
    token = q.claim(run_dir, "w1")
    with pytest.raises(q.OwnershipLost):
        q.finish(run_dir, {**token, "ts": token["ts"] + 1}, "done")
    assert json.loads((run_dir / "queue.json").read_text())["items"][0]["state"] == "in_progress"


def test_manifest_change_is_refused(home):
    run_dir = make_run(["login", "cart", "checkout"])
    with pytest.raises(q.ManifestMismatch):
        q.init_queue(run_dir, ["login", "cart"])
    (run_dir / "manifest.txt").write_text("login\ncart\n")
    with pytest.raises(harvest.DenominatorError):
        harvest.harvest(run_dir)


def test_resource_wait_backs_off_then_exhausts(home):
    run_dir = make_run(["login"])
    now = 1000.0
    for delay in q.BACKOFF:
        token = q.claim(run_dir, "w1", now=now)
        assert q.resource_wait(run_dir, token, now=now) == "pending"
        assert q.claim(run_dir, "w1", now=now + delay - 1) is None
        now += delay
    token = q.claim(run_dir, "w1", now=now)
    assert q.resource_wait(run_dir, token, now=now) == "failed"
    item = json.loads((run_dir / "queue.json").read_text())["items"][0]
    assert item["failure"]["code"] == ev.Code.RESOURCE_EXHAUSTED and item["wait_count"] == 4


def test_slow_liveness_check_runs_outside_the_lock(home, monkeypatch):
    run_dir = make_run(["login"])
    q.claim(run_dir, "w1")
    probes = []

    def probe(pid):  # a second handle must be able to take the lock while we check liveness
        fd = os.open(run_dir / "queue.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            probes.append("free")
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            probes.append("held")
        finally:
            os.close(fd)
        return False

    monkeypatch.setattr(q, "pid_alive", probe)
    assert q.reclaim_stale(run_dir) == ["login"]
    assert probes == ["free"]


def test_reclaim_dead_owner_dry_run_first_then_releases_its_lease(home):
    run_dir = make_run(["login"])
    q.init_pool(run_dir, ["http://a"])
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    token = q.claim(run_dir, "w1")
    q.acquire(run_dir, token)
    data = json.loads((run_dir / "queue.json").read_text())
    data["items"][0]["pid"] = dead.pid
    ev.atomic_write_json(run_dir / "queue.json", data)

    assert q.reclaim_stale(run_dir, dry_run=True) == ["login"]
    assert json.loads((run_dir / "queue.json").read_text())["items"][0]["state"] == "in_progress"
    assert q.reclaim_stale(run_dir) == ["login"]
    assert json.loads((run_dir / "queue.json").read_text())["items"][0]["state"] == "pending"
    assert not list((run_dir / "leases").glob("*.json"))
    events = [json.loads(ln) for ln in (run_dir / "events.jsonl").read_text().splitlines()]
    assert any(e["event"] == "reclaim" and e["previous"]["pid"] == dead.pid for e in events)


def test_stop_is_checked_at_stage_boundaries(home, monkeypatch):
    run_dir = make_run(["login"])
    launched = []

    def fake_run(feature, url, budget=None):  # STOP arrives while TRYOUT is running
        launched.append(feature)
        (run_dir / "STOP").touch()
        eid = fake_packet(feature)
        return eid, {c["case"]: c for c in json.loads(ev.packet_path(eid).read_text())["cases"]}

    monkeypatch.setattr(execution, "run_feature", fake_run)
    with pytest.raises(execution.StopRequested):
        execution.process_item(run_dir, "login", "http://unused", execution.Budget())
    assert launched == ["login"]  # APPROVE never started


def test_repeated_same_failure_pauses_on_budget(home, monkeypatch):
    run_dir = make_run(["login"])
    q.init_pool(run_dir, ["http://a"])

    def failing_run(feature, url, budget=None):
        eid = fake_packet(feature, observed_override={"LOGIN-0001#1": "500 Internal Server Error"})
        return eid, {c["case"]: c for c in json.loads(ev.packet_path(eid).read_text())["cases"]}

    monkeypatch.setattr(execution, "run_feature", failing_run)
    execution.handle(run_dir, q.claim(run_dir, "w1"), budget_limits={"same_failure": 2})
    item = json.loads((run_dir / "queue.json").read_text())["items"][0]
    assert item["failure"]["code"] == ev.Code.PAUSED_BUDGET
    assert harvest.harvest(run_dir)["items"]["login"]["outcome"] == "WAITING_DECISION"
    assert not list((run_dir / "leases").glob("*.json"))


def test_secrets_are_masked_in_events(home):
    run_dir = make_run(["login"])
    ev.append_event(run_dir, event="login", form={"user": "demo", "password": "demo123", "Cookie": "abc"})
    line = (run_dir / "events.jsonl").read_text()
    assert "demo123" not in line and "abc" not in line and '"user": "demo"' in line
