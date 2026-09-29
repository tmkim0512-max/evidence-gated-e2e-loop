"""The four false-success patterns. scripts/red_check.py removes each guard and expects these to fail."""
import subprocess
import sys
import time

import pytest

from loop import evidence as ev
from loop import harvest
from loop import queue as q
from tests.conftest import fake_packet, make_run


# 1. Blocked-reason abuse: "couldn't do it" dressed up as an external block.
@pytest.mark.parametrize("reason", ["script missing", "404", "login failed", "test failed"])
def test_block_reason_abuse_is_counted_as_loop_failure(home, reason):
    outcome, record = ev.classify_block(reason, ev.Owner.SERVICE)
    assert outcome == ev.Outcome.FAILED and record["owner"] == ev.Owner.LOOP

    run_dir = make_run(["login"])
    token = q.claim(run_dir, "w1")
    q.finish(run_dir, token, "failed", ev.failure(ev.Code.EXTERNAL_BLOCK, ev.Owner.SERVICE, reason))
    rep = harvest.harvest(run_dir)
    assert rep["items"]["login"]["outcome"] == "FAILED"
    assert rep["counts"]["BLOCKED_EXTERNAL"] == 0


def test_whitelisted_block_is_external(home):
    assert ev.classify_block("payment", ev.Owner.SERVICE)[0] == ev.Outcome.BLOCKED_EXTERNAL
    assert ev.classify_block("payment", ev.Owner.LOOP)[0] == ev.Outcome.FAILED  # the loop can't blame itself away


# 2. Stage stamp without evidence: marking TRYOUT done with no result file.
@pytest.mark.parametrize("runs", [[], ["x-never-ran"], ["empty"]])
def test_stage_stamp_without_tryout_packet_is_refused(home, runs):
    if runs == ["empty"]:
        ev.packet_path("empty").parent.mkdir(parents=True)
        ev.packet_path("empty").write_text("{}")
    ev.transition("login", "LOGIN-0001", "authored")
    with pytest.raises(ev.LedgerError):
        ev.transition("login", "LOGIN-0001", "verified", runs=runs)
    assert ev.current("login", "LOGIN-0001")["to"] == "authored"


def test_stage_stamp_with_tryout_packet_is_accepted(home):
    ev.transition("login", "LOGIN-0001", "authored")
    ev.transition("login", "LOGIN-0001", "verified", runs=[fake_packet("login")])
    assert ev.current("login", "LOGIN-0001")["to"] == "verified"


# 3. Resource release undecidable: release must really free mine and provably leave others'.
def test_release_frees_my_lease_and_skips_someone_elses(home):
    run_dir = make_run(["login", "cart"])
    q.init_pool(run_dir, ["http://a", "http://b"])
    mine, theirs = q.claim(run_dir, "w1"), q.claim(run_dir, "w2")
    assert q.acquire(run_dir, mine) == "http://a"
    assert q.acquire(run_dir, theirs) == "http://b"

    assert q.release_all(run_dir, mine) == ["0.json"]
    assert not (run_dir / "leases" / "0.json").exists()
    assert (run_dir / "leases" / "1.json").exists()
    assert q.release(run_dir, run_dir / "leases" / "1.json", mine) is False


# 4. Zombie read as alive: an exited, unreaped child must not count as a live worker.
def _wait_zombie(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
        if stat.strip().startswith("Z"):
            return
        time.sleep(0.05)
    pytest.fail("child never became a zombie")


def test_zombie_child_is_not_alive():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    _wait_zombie(child.pid)
    assert q.pid_alive(child.pid) is False
    child.wait()


def test_running_child_is_alive():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert q.pid_alive(child.pid) is True
    finally:
        child.kill()
        child.wait()
