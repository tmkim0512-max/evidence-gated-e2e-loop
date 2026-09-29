"""Run testware, walk one item through AUTHOR -> TRYOUT -> APPROVE, host workers and replay.

Replay never imports loop.verify: the regression path has no reviewer or AI code in it.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import evidence as ev
from . import harvest
from . import queue as q

LIMITS = {"seconds": 900, "launches": 12, "same_failure": 2, "leases": 3}


class BudgetExceeded(Exception):
    pass


class StopRequested(Exception):
    pass


class ItemFailed(Exception):
    def __init__(self, record):
        super().__init__(record["code"])
        self.record = record


class Budget:
    def __init__(self, limits=None):
        self.limits = {**LIMITS, **(limits or {})}
        self.used = dict.fromkeys(self.limits, 0)
        self.start = time.monotonic()

    def charge(self, kind: str) -> None:
        self.used[kind] += 1
        if self.used[kind] > self.limits[kind]:
            raise BudgetExceeded(f"{kind} {self.used[kind]}>{self.limits[kind]}")

    def check_time(self) -> None:
        if time.monotonic() - self.start > self.limits["seconds"]:
            raise BudgetExceeded("seconds")


def checkpoint(run_dir: Path, budget: Budget) -> None:
    """Called at every stage boundary, not only between items."""
    if q.stop_requested(run_dir):
        raise StopRequested
    budget.check_time()


def app_healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/__health", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def run_feature(feature: str, url: str, budget: Budget | None = None):
    """One pytest run -> observation packet. The script's own pass/fail is deliberately not recorded."""
    exec_id = ev.new_id("x")
    out = ev.home() / "exec" / exec_id
    out.mkdir(parents=True)
    signal = None if app_healthy(url) else "app_unreachable"
    if not signal:
        if budget:
            budget.charge("launches")
        env = {**os.environ, "BASE_URL": url, "EVIDENCE_DIR": str(out.resolve())}
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--confcutdir={ev.testware()}",
               str(ev.script_path(feature))]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
        (out / "pytest.log").write_text(proc.stdout + proc.stderr)
    cases = []
    for case in ev.load_cases(feature)["cases"]:
        steps = []
        for s in case["steps"]:
            obs_file = out / case["id"] / f"{s['n']}.json"
            obs = json.loads(obs_file.read_text()) if obs_file.exists() else {}
            steps.append({"key": f"{case['id']}#{s['n']}", "expect": s["expect"], "observed": obs.get("observed"),
                          "screenshot": obs.get("screenshot"), "blocked_signal": signal})
        cases.append({"case": case["id"], "steps": steps})
    ev.atomic_write_json(out / "packet.json", {"run_id": exec_id, "feature": feature, "cases": cases})
    return exec_id, {c["case"]: c for c in cases}


def first_miss(case_packet: dict):
    """Mechanical check: None if every step's expectation is in its observation and a screenshot exists."""
    for s in case_packet["steps"]:
        shot_ok = bool(s["screenshot"]) and Path(s["screenshot"]).exists()
        if s["observed"] is None or s["expect"] not in s["observed"] or not shot_ok:
            return s
    return None


def matches_exception(case_packet: dict, exc) -> bool:
    """A registered known defect is excluded only if it fails exactly the registered way."""
    if not exc:
        return False
    miss = first_miss(case_packet)
    return bool(miss) and miss["key"].endswith(f"#{exc['step']}") and exc["observed"] in (miss["observed"] or "")


def process_item(run_dir: Path, feature: str, url: str, budget: Budget, reviewers=None) -> None:
    checkpoint(run_dir, budget)
    # AUTHOR: scripts are checked in; this stage only proves they exist and match the case records.
    doc = ev.load_cases(feature) if (ev.testware() / feature / "cases.json").exists() else None
    if doc is None or not ev.script_path(feature).exists():
        raise ItemFailed(ev.failure(ev.Code.SCRIPT_MISSING, ev.Owner.LOOP, f"{feature}: cases.json or script missing",
                                    resume_from="AUTHOR"))
    if "blocked" in doc:
        _, record = ev.classify_block(doc["blocked"]["reason"], doc["blocked"]["owner"])
        raise ItemFailed(record)
    case_ids = [c["id"] for c in doc["cases"]]
    for cid in case_ids:
        cur = ev.current(feature, cid)
        if cur is None or (cur["to"] in ("approved", "stable", "drift") and cur["hashes"] != ev.file_hashes(feature)):
            ev.transition(feature, cid, "authored", note="new" if cur is None else "files changed since approval")
    excs = ev.load_exceptions(feature)

    # TRYOUT: one run; everything except registered exceptions must pass mechanically.
    while True:
        checkpoint(run_dir, budget)
        exec_id, packets = run_feature(feature, url, budget)
        bad = [c for c in case_ids if first_miss(packets[c]) and not matches_exception(packets[c], excs.get(c))]
        if not bad:
            break
        ev.append_event(run_dir, event="tryout_failed", item=feature, exec_id=exec_id, cases=bad)
        budget.charge("same_failure")
    for cid in case_ids:
        if ev.current(feature, cid)["to"] == "authored":
            ev.transition(feature, cid, "verified", runs=[exec_id], note="known-defect exception" if cid in excs else None)

    # APPROVE: two further independent runs + mechanical check + blind two-reviewer review.
    pending = [c for c in case_ids if c not in excs and ev.current(feature, c)["to"] == "verified"]
    if not pending:
        return
    runs = []
    for _ in range(2):
        checkpoint(run_dir, budget)
        runs.append(run_feature(feature, url, budget))
    from . import verify  # imported here so the replay path never loads reviewer code
    for cid in pending:
        if any(first_miss(pk[cid]) for _, pk in runs):
            ev.append_event(run_dir, event="approval_run_failed", item=feature, case=cid)
            continue
        checkpoint(run_dir, budget)
        budget.charge("launches")
        review = verify.review_case(runs[-1][1][cid], reviewers)
        ids = [r for r, _ in runs]
        if review["result"] == "agreed":
            ev.transition(feature, cid, "approved", runs=ids, review=review)
        else:
            ev.transition(feature, cid, "verified", runs=ids, review=review, note="review not agreed")


def handle(run_dir: Path, token: dict, budget_limits=None, reviewers=None) -> None:
    budget = Budget(budget_limits)
    try:
        budget.charge("leases")
        url = q.acquire(run_dir, token)
        if url is None:
            q.resource_wait(run_dir, token)
            return
        process_item(run_dir, token["item"], url, budget, reviewers)
        q.finish(run_dir, token, "done")
    except BudgetExceeded as e:
        q.finish(run_dir, token, "failed", ev.failure(ev.Code.PAUSED_BUDGET, ev.Owner.LOOP, str(e)))
    except StopRequested:
        q.finish(run_dir, token, "pending", ev.failure(ev.Code.STOPPED, ev.Owner.HUMAN, "STOP file"))
    except ItemFailed as e:
        q.finish(run_dir, token, "failed", e.record)
    except q.OwnershipLost:
        pass  # already logged as an event by the queue; the new owner's record wins
    except Exception as e:  # recorded as a loop failure, never swallowed
        ev.append_event(run_dir, event="loop_error", item=token["item"], error=repr(e))
        q.finish(run_dir, token, "failed", ev.failure(ev.Code.LOOP_ERROR, ev.Owner.LOOP, repr(e)))
    finally:
        q.release_all(run_dir, token)


def worker_main(run_dir: Path, name: str) -> int:
    while not q.stop_requested(run_dir):
        token = q.claim(run_dir, name)
        if token is None:
            if q.all_settled(run_dir):
                return 0
            time.sleep(0.2)
            continue
        handle(run_dir, token)
        harvest.check(run_dir)  # recompute from files after every item; raises if the sum breaks
    return 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_apps(n: int):
    apps, urls = [], []
    for _ in range(n):
        port = free_port()
        apps.append(subprocess.Popen([sys.executable, "-m", "sample_app.server", "--port", str(port)]))
        urls.append(f"http://127.0.0.1:{port}")
    deadline = time.monotonic() + 15
    while not all(app_healthy(u) for u in urls):
        if time.monotonic() > deadline:
            stop_procs(apps)
            raise RuntimeError("sample app did not come up")
        time.sleep(0.1)
    return apps, urls


def stop_procs(procs) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def run(run_id: str, workers: int, manifest: Path) -> Path:
    run_dir = ev.home() / "runs" / run_id
    items = ev.read_manifest(manifest)
    run_dir.mkdir(parents=True, exist_ok=True)
    frozen = run_dir / "manifest.txt"
    if not frozen.exists():
        frozen.write_text("\n".join(items) + "\n")
    q.init_queue(run_dir, items)  # a resumed run whose manifest differs is refused: the denominator is frozen
    apps, urls = start_apps(workers)
    procs = []
    try:
        q.init_pool(run_dir, urls)
        procs = [subprocess.Popen([sys.executable, "-m", "loop.execution", str(run_dir), f"w{i + 1}"])
                 for i in range(workers)]
        while any(p.poll() is None for p in procs):
            time.sleep(0.2)
        if any(p.returncode != 0 for p in procs):
            q.reclaim_stale(run_dir)
    finally:
        stop_procs(procs + apps)
    harvest.check(run_dir)
    return run_dir


def replay(manifest: Path) -> list[tuple]:
    apps, urls = start_apps(1)
    results = []
    try:
        for feature in ev.read_manifest(manifest):
            doc = ev.load_cases(feature)
            ready = [c["id"] for c in doc["cases"] if (cur := ev.current(feature, c["id"]))
                     and cur["to"] in ("approved", "stable", "drift") and cur["hashes"] == ev.file_hashes(feature)]
            if not ready:
                continue
            exec_id, packets = run_feature(feature, urls[0])
            for cid in ready:
                miss = first_miss(packets[cid])
                before = ev.current(feature, cid)["to"]
                ev.transition(feature, cid, "drift" if miss else "stable", runs=[exec_id])
                results.append((feature, cid, before, "drift" if miss else "stable", miss))
    finally:
        stop_procs(apps)
    return results


if __name__ == "__main__":
    sys.exit(worker_main(Path(sys.argv[1]), sys.argv[2]))
