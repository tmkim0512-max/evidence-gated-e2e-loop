"""Prove each false-success test is real: remove the guard, the test must fail; restore, it must pass.

Usage: python scripts/red_check.py   (exit 1 if any test stays green without its guard)
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
T = "tests/test_false_success.py::"
MUTATIONS = [
    ("1 block-reason abuse: accept any reason", "loop/evidence.py",
     [("    if owner in EXTERNAL_OWNERS and reason in BLOCK_REASONS:\n", "    if True:\n")],
     T + "test_block_reason_abuse_is_counted_as_loop_failure"),
    ("2 stage stamp: no packet check", "loop/evidence.py",
     [('    if to != "authored" and not (runs and all(packet_ok(r) for r in runs)):\n', "    if False:\n")],
     T + "test_stage_stamp_without_tryout_packet_is_refused"),
    ("3a release: skip ownership check", "loop/queue.py",
     [('        if json.loads(lease.read_text()).get("owner") != token["attempt"]:\n            return False\n', "")],
     T + "test_release_frees_my_lease_and_skips_someone_elses"),
    ("3b release: report success without unlinking", "loop/queue.py",
     [("        lease.unlink()\n        return True\n", "        return True\n")],
     T + "test_release_frees_my_lease_and_skips_someone_elses"),
    ("4 liveness: bare kill(pid, 0)", "loop/queue.py",
     [("    if _reap(pid):\n        return False\n", ""), ("    return not _is_zombie(pid)\n", "    return True\n")],
     T + "test_zombie_child_is_not_alive"),
]


def pytest(node: str):
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
                       cwd=REPO, capture_output=True, text=True)
    first_error = next((ln.strip() for ln in p.stdout.splitlines() if ln.startswith("E ")), "")
    return p.returncode, first_error


def main() -> int:
    ok = True
    for name, rel, edits, node in MUTATIONS:
        path = REPO / rel
        original = path.read_text()
        mutated = original
        for old, new in edits:
            assert mutated.count(old) == 1, f"{name}: guard text not found exactly once in {rel}"
            mutated = mutated.replace(old, new)
        try:
            path.write_text(mutated)
            red_rc, why = pytest(node)
        finally:
            path.write_text(original)
        green_rc, _ = pytest(node)
        good = red_rc != 0 and green_rc == 0
        ok &= good
        print(f"{'OK ' if good else 'BAD'} guard removed -> {'RED' if red_rc else 'green'} | restored -> "
              f"{'GREEN' if green_rc == 0 else 'red'} | {name}\n      {why[:150]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
