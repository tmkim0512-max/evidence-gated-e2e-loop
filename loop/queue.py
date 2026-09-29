"""Durable file queue (work-stealing claims, backoff, STOP, stale-owner reclaim) and a lease pool.

Rule: never run a subprocess while holding a lock. Slow checks follow
snapshot (in lock) -> check (outside) -> revalidate and write (in lock).
"""
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from . import evidence as ev

BACKOFF = (300, 600, 1200)  # seconds; a 4th RESOURCE_WAIT ends the item as RESOURCE_EXHAUSTED
TOKEN_KEYS = ("item", "worker", "pid", "ts", "attempt")


class ManifestMismatch(Exception):
    pass


class OwnershipLost(Exception):
    pass


def _load(run_dir: Path) -> dict:
    return json.loads((run_dir / "queue.json").read_text())


def _save(run_dir: Path, data: dict) -> None:
    ev.atomic_write_json(run_dir / "queue.json", data)


def _find(data: dict, item: str) -> dict:
    return next(it for it in data["items"] if it["item"] == item)


def _owned(it: dict, token: dict) -> bool:
    return it["state"] == "in_progress" and all(it[k] == token[k] for k in TOKEN_KEYS)


def init_queue(run_dir: Path, items: list[str]) -> None:
    with ev.locked(run_dir / "queue.lock"):
        if (run_dir / "queue.json").exists():
            have = sorted(it["item"] for it in _load(run_dir)["items"])
            if have != sorted(items):
                raise ManifestMismatch(f"queue has {have}, manifest has {sorted(items)}")
            return
        blank = {"state": "pending", "wait_count": 0, "eligible_at": 0, "worker": None, "pid": None,
                 "ts": None, "attempt": None, "failure": None}
        _save(run_dir, {"version": 1, "items": [{"item": i, **blank} for i in items]})


def claim(run_dir: Path, worker: str, now: float | None = None):
    now = time.time() if now is None else now
    with ev.locked(run_dir / "queue.lock"):
        data = _load(run_dir)
        for it in data["items"]:
            if it["state"] == "pending" and it["eligible_at"] <= now:
                it.update(state="in_progress", worker=worker, pid=os.getpid(), ts=now, attempt=uuid.uuid4().hex[:8])
                _save(run_dir, data)
                token = {k: it[k] for k in TOKEN_KEYS}
                break
        else:
            return None
    ev.append_event(run_dir, event="claim", **token)
    return token


def _settle(run_dir: Path, token: dict, update) -> dict:
    with ev.locked(run_dir / "queue.lock"):
        data = _load(run_dir)
        it = _find(data, token["item"])
        if not _owned(it, token):  # someone reclaimed it: our result would overwrite theirs
            ev.append_event(run_dir, event="ownership_lost", token=token, now_owner={k: it[k] for k in TOKEN_KEYS})
            raise OwnershipLost(token["item"])
        update(it)
        _save(run_dir, data)
        return dict(it)


def finish(run_dir: Path, token: dict, state: str, failure=None) -> None:
    it = _settle(run_dir, token, lambda it: it.update(state=state, failure=failure, pid=None))
    ev.append_event(run_dir, event="finish", item=it["item"], attempt=token["attempt"], state=state, failure=failure)


def resource_wait(run_dir: Path, token: dict, now: float | None = None) -> str:
    """Waiting for a resource is not a failure: back off and requeue until the backoff is used up."""
    now = time.time() if now is None else now

    def update(it):
        it["wait_count"] += 1
        if it["wait_count"] > len(BACKOFF):
            it.update(state="failed", pid=None, failure=ev.failure(ev.Code.RESOURCE_EXHAUSTED, ev.Owner.LOOP))
        else:
            it.update(state="pending", pid=None, eligible_at=now + BACKOFF[it["wait_count"] - 1])

    it = _settle(run_dir, token, update)
    ev.append_event(run_dir, event="resource_wait", item=it["item"], wait_count=it["wait_count"], state=it["state"])
    return it["state"]


def stop_requested(run_dir: Path) -> bool:
    return (run_dir / "STOP").exists()


def all_settled(run_dir: Path) -> bool:
    return all(it["state"] not in ("pending", "in_progress") for it in _load(run_dir)["items"])


def _reap(pid: int) -> bool:
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except ChildProcessError:  # not our child: nothing to reap
        return False


def _is_zombie(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return out.strip().startswith("Z")


def pid_alive(pid: int) -> bool:
    """kill(pid, 0) alone reports an exited-but-unreaped child (zombie) as alive."""
    if _reap(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


def reclaim_stale(run_dir: Path, dry_run: bool = False) -> list[str]:
    with ev.locked(run_dir / "queue.lock"):
        snapshot = [dict(it) for it in _load(run_dir)["items"] if it["state"] == "in_progress"]
    dead = [s for s in snapshot if not pid_alive(s["pid"])]  # slow check, outside the lock
    if dry_run:
        return [s["item"] for s in dead]
    reclaimed = []
    with ev.locked(run_dir / "queue.lock"):
        data = _load(run_dir)
        for s in dead:
            it = _find(data, s["item"])
            if _owned(it, s):  # revalidate: still the same dead owner
                it.update(state="pending", worker=None, pid=None)
                reclaimed.append(s)
        _save(run_dir, data)
    for s in reclaimed:
        ev.append_event(run_dir, event="reclaim", item=s["item"], previous=s)
        release_all(run_dir, s)
    return [s["item"] for s in reclaimed]


def requeue_failed(run_dir: Path, dry_run: bool = False) -> list[str]:
    with ev.locked(run_dir / "queue.lock"):
        data = _load(run_dir)
        failed = [it for it in data["items"] if it["state"] == "failed"]
        if not dry_run:
            for it in failed:
                ev.append_event(run_dir, event="requeue", item=it["item"], previous=dict(it))
                it.update(state="pending", wait_count=0, eligible_at=0, failure=None)
            _save(run_dir, data)
    return [it["item"] for it in failed]


# --- lease pool: one sample-app instance per lease ---------------------------------------------

def init_pool(run_dir: Path, urls: list[str]) -> None:
    ev.atomic_write_json(run_dir / "pool.json", urls)
    (run_dir / "leases").mkdir(parents=True, exist_ok=True)


def acquire(run_dir: Path, token: dict):
    with ev.locked(run_dir / "pool.lock"):
        for i, url in enumerate(json.loads((run_dir / "pool.json").read_text())):
            lease = run_dir / "leases" / f"{i}.json"
            if not lease.exists():
                ev.atomic_write_json(lease, {"url": url, "owner": token["attempt"], "worker": token["worker"]})
                return url
    return None


def release(run_dir: Path, lease: Path, token: dict) -> bool:
    """True only if this owner's lease file was actually removed; someone else's lease is left alone."""
    with ev.locked(run_dir / "pool.lock"):
        if not lease.exists():
            return False
        if json.loads(lease.read_text()).get("owner") != token["attempt"]:
            return False
        lease.unlink()
        return True


def release_all(run_dir: Path, token: dict) -> list[str]:
    leases = sorted((run_dir / "leases").glob("*.json")) if (run_dir / "leases").exists() else []
    return [p.name for p in leases if release(run_dir, p, token)]
