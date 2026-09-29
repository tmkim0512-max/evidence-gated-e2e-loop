"""Harvester recomputation: denominator, self-report, hash lock, approval runs."""
import json
import random

import pytest

from loop import evidence as ev
from loop import harvest
from loop import queue as q
from tests.conftest import approve_all, fake_packet, make_run


@pytest.mark.parametrize("seed", range(30))
def test_outcomes_always_sum_to_the_denominator(home, seed):
    rng = random.Random(seed)
    items = ["login", "cart", "checkout"]
    run_dir = make_run(items)
    data = json.loads((run_dir / "queue.json").read_text())
    codes = [ev.Code.PAUSED_BUDGET, ev.Code.EXTERNAL_BLOCK, ev.Code.LOOP_ERROR, ev.Code.RESOURCE_EXHAUSTED]
    for it in data["items"]:
        it["state"] = rng.choice(["pending", "in_progress", "failed", "done"])
        if it["state"] == "failed":
            it["failure"] = ev.failure(rng.choice(codes), rng.choice(list(ev.Owner)), rng.choice(["payment", "404"]))
    ev.atomic_write_json(run_dir / "queue.json", data)
    if rng.random() < 0.5:
        approve_all("login")
    rep = harvest.harvest(run_dir)
    assert sum(rep["counts"].values()) == rep["denominator"] == 3
    assert set(rep["counts"]) == {o.value for o in ev.Outcome}


def test_worker_saying_success_is_not_success(home, capsys):
    run_dir = make_run(["login"])
    token = q.claim(run_dir, "w1")
    print("SUCCESS: all tests passed")  # the worker's self-report
    q.finish(run_dir, token, "done")
    item = harvest.harvest(run_dir)["items"]["login"]
    assert item["outcome"] == "FAILED" and item["failure"]["code"] == ev.Code.NO_APPROVAL


def test_approved_with_evidence_is_success(home):
    run_dir = make_run(["login"])
    approve_all("login")
    q.finish(run_dir, q.claim(run_dir, "w1"), "done")
    assert harvest.harvest(run_dir)["items"]["login"]["outcome"] == "SUCCESS"


def test_one_byte_change_after_approval_is_evidence_mismatch(home):
    run_dir = make_run(["login"])
    approve_all("login")
    q.finish(run_dir, q.claim(run_dir, "w1"), "done")
    script = ev.script_path("login")
    script.write_bytes(script.read_bytes() + b" ")
    item = harvest.harvest(run_dir)["items"]["login"]
    assert item["outcome"] == "FAILED" and item["failure"]["code"] == ev.Code.EVIDENCE_MISMATCH


def test_deleted_packet_is_evidence_lost(home):
    run_dir = make_run(["login"])
    approve_all("login")
    q.finish(run_dir, q.claim(run_dir, "w1"), "done")
    ev.packet_path(ev.last_approval("login", "LOGIN-0001")["runs"][0]).unlink()
    item = harvest.harvest(run_dir)["items"]["login"]
    assert item["outcome"] == "FAILED" and item["failure"]["code"] == ev.Code.EVIDENCE_LOST


def test_approval_with_the_same_run_twice_is_refused(home):
    ev.transition("login", "LOGIN-0001", "authored")
    ev.transition("login", "LOGIN-0001", "verified", runs=[fake_packet("login")])
    run = fake_packet("login")
    with pytest.raises(ev.LedgerError, match="two distinct runs"):
        ev.transition("login", "LOGIN-0001", "approved", runs=[run, run], review={"result": "agreed"})


def test_approval_without_agreed_review_is_refused(home):
    ev.transition("login", "LOGIN-0001", "authored")
    ev.transition("login", "LOGIN-0001", "verified", runs=[fake_packet("login")])
    with pytest.raises(ev.LedgerError, match="agreed blind review"):
        ev.transition("login", "LOGIN-0001", "approved", runs=[fake_packet("login"), fake_packet("login")],
                      review={"result": "disagree"})
