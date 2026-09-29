"""loop run | replay | report | stop | recover"""
import argparse
import os
import sys
from pathlib import Path

from . import evidence as ev
from . import execution, harvest
from . import queue as q


def latest_run(run_id=None) -> Path:
    if run_id:
        return ev.home() / "runs" / run_id
    runs = sorted((ev.home() / "runs").glob("r-*"), key=lambda p: p.stat().st_mtime)
    if not runs:
        sys.exit("no runs yet: loop run first")
    return runs[-1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="loop")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--workers", type=int, default=3)
    r.add_argument("--run-id")
    sub.add_parser("replay")
    for name in ("report", "stop"):
        sub.add_parser(name).add_argument("--run-id")
    rc = sub.add_parser("recover")
    rc.add_argument("--run-id")
    rc.add_argument("--reclaim-stale", action="store_true")
    rc.add_argument("--requeue-failed", action="store_true")
    rc.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    manifest = Path(os.environ.get("LOOP_MANIFEST", "manifest.txt"))

    if a.cmd == "run":
        run_dir = execution.run(a.run_id or ev.new_id("r"), a.workers, manifest)
        print(harvest.to_markdown(harvest.check(run_dir)))
    elif a.cmd == "report":
        print(harvest.to_markdown(harvest.check(latest_run(a.run_id))))
    elif a.cmd == "stop":
        (latest_run(a.run_id) / "STOP").touch()
        print("STOP requested")
    elif a.cmd == "recover":
        run_dir = latest_run(a.run_id)
        tag = " (dry-run)" if a.dry_run else ""
        if a.reclaim_stale:
            print(f"reclaimed{tag}: {q.reclaim_stale(run_dir, a.dry_run)}")
        if a.requeue_failed:
            print(f"requeued{tag}: {q.requeue_failed(run_dir, a.dry_run)}")
    elif a.cmd == "replay":
        for feature, case, before, after, miss in execution.replay(manifest):
            why = f'   (expected "{miss["expect"]}" / observed "{miss["observed"]}")' if miss else ""
            print(f"{feature}/{case}  {before} -> {after}{why}")
        loaded = "loop.verify" in sys.modules
        print("reviewer/AI calls: " + ("LOADED - bug" if loaded else "0 (loop.verify never imported)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
