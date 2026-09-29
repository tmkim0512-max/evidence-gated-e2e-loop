"""End to end against the sample app in a real browser: run, then replay with a planted bug."""
import os
import subprocess
import sys

from loop import execution, harvest
from tests.conftest import REPO


def test_run_then_replay_catches_drift(home):
    run_dir = execution.run("r-e2e", 3, REPO / "manifest.txt")
    rep = harvest.harvest(run_dir)
    assert rep["counts"]["SUCCESS"] == 2 and rep["counts"]["PARTIAL"] == 1
    assert rep["items"]["cart"]["exceptions"] == ["CART-0002"]
    assert rep["cases"]["approved"] == 7 and rep["cases"]["total"] == 8

    out = subprocess.run([sys.executable, "-m", "loop.cli", "replay"], capture_output=True, text=True,
                         env={**os.environ, "BUG": "cart_total", "LOOP_MANIFEST": str(REPO / "manifest.txt")},
                         cwd=REPO, timeout=300).stdout
    assert 'cart/CART-0001  approved -> drift   (expected "Total: 30,000" / observed "Total: 25,000")' in out
    assert "login/LOGIN-0001  approved -> stable" in out
    assert "reviewer/AI calls: 0" in out
    assert harvest.harvest(run_dir)["items"]["cart"]["cases"]["CART-0001"] == "drift"
