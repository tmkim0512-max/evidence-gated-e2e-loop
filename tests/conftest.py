import json
import shutil
from pathlib import Path

import pytest

from loop import evidence as ev
from loop import queue as q

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated state dir + a private copy of the testware (so tests may tamper with it)."""
    monkeypatch.setenv("LOOP_HOME", str(tmp_path / "state"))
    shutil.copytree(REPO / "testware", tmp_path / "testware", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setenv("LOOP_TESTWARE", str(tmp_path / "testware"))
    return tmp_path


def make_run(items, name="r-test") -> Path:
    run_dir = ev.home() / "runs" / name
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.txt").write_text("\n".join(items) + "\n")
    q.init_queue(run_dir, items)
    return run_dir


def fake_packet(feature: str, observed_override=None) -> str:
    """Write an execution packet as a real run would; every step observes its expectation."""
    exec_id = ev.new_id("x")
    out = ev.packet_path(exec_id).parent
    out.mkdir(parents=True)
    shot = out / "shot.png"
    shot.write_bytes(b"png")
    cases = [{"case": c["id"], "steps": [
        {"key": f"{c['id']}#{s['n']}", "expect": s["expect"],
         "observed": (observed_override or {}).get(f"{c['id']}#{s['n']}", s["expect"]),
         "screenshot": str(shot), "blocked_signal": None} for s in c["steps"]]}
        for c in ev.load_cases(feature)["cases"]]
    ev.packet_path(exec_id).write_text(json.dumps({"run_id": exec_id, "feature": feature, "cases": cases}))
    return exec_id


def approve_all(feature: str) -> None:
    for c in ev.load_cases(feature)["cases"]:
        ev.transition(feature, c["id"], "authored")
        ev.transition(feature, c["id"], "verified", runs=[fake_packet(feature)])
        ev.transition(feature, c["id"], "approved", runs=[fake_packet(feature), fake_packet(feature)],
                      review={"result": "agreed"})
