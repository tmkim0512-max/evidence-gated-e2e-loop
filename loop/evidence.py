"""Records, atomic files, the event log and the ledger.

The ledger (``ledger.jsonl``) is the only place a test case's state lives.
Every transition past ``authored`` must point at execution packets that exist on disk.
"""
import enum
import fcntl
import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Verdict(enum.StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_RUN = "NOT_RUN"


class Outcome(enum.StrEnum):  # exactly one per manifest item; the seven must sum to the denominator
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    WAITING_DECISION = "WAITING_DECISION"
    RUNNING = "RUNNING"
    NOT_STARTED = "NOT_STARTED"


class Owner(enum.StrEnum):
    PRODUCT = "product"
    SPEC = "spec"
    SERVICE = "service"
    LOOP = "loop"
    HUMAN = "human"


class Code(enum.StrEnum):
    SCRIPT_MISSING = "SCRIPT_MISSING"
    EXTERNAL_BLOCK = "EXTERNAL_BLOCK"
    BLOCK_REASON_REJECTED = "BLOCK_REASON_REJECTED"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    STOPPED = "STOPPED"
    LOOP_ERROR = "LOOP_ERROR"
    EVIDENCE_MISMATCH = "EVIDENCE_MISMATCH"
    EVIDENCE_LOST = "EVIDENCE_LOST"
    NO_APPROVAL = "NO_APPROVAL"


# "script missing", "404", "login failed", "test failed" are work to do or defects - never blocks.
BLOCK_REASONS = {"permission", "payment", "provisioning"}
EXTERNAL_OWNERS = {Owner.PRODUCT, Owner.SERVICE}
SECRET_KEYS = ("password", "token", "cookie", "secret", "authorization")


class LedgerError(Exception):
    pass


def home() -> Path:
    return Path(os.environ.get("LOOP_HOME", "state"))


def testware() -> Path:
    return Path(os.environ.get("LOOP_TESTWARE", "testware"))


def failure(code, owner, reason="", resume_from=None, evidence=None):
    return {"code": code, "owner": owner, "reason": reason, "resume_from": resume_from, "evidence": evidence}


def classify_block(reason, owner):
    """A block request is honoured only for a whitelisted reason with an external owner."""
    if owner in EXTERNAL_OWNERS and reason in BLOCK_REASONS:
        return Outcome.BLOCKED_EXTERNAL, failure(Code.EXTERNAL_BLOCK, owner, reason)
    return Outcome.FAILED, failure(Code.BLOCK_REASON_REJECTED, Owner.LOOP, reason)


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def mask(obj):
    if isinstance(obj, dict):
        return {k: "***" if any(s in k.lower() for s in SECRET_KEYS) else mask(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(v) for v in obj]
    return obj


def append_event(run_dir: Path, **fields) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(mask({"ts": time.time(), "run_id": run_dir.name, **fields}), ensure_ascii=False)
    with open(run_dir / "events.jsonl", "a") as f:
        f.write(line + "\n")


def new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def read_manifest(path: Path) -> list[str]:
    items = [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    if len(items) != len(set(items)):
        raise ValueError(f"duplicate entries in {path}")
    return items


def load_cases(feature: str) -> dict:
    return json.loads((testware() / feature / "cases.json").read_text())


def load_exceptions(feature: str) -> dict:
    path = testware() / feature / "exceptions.json"
    return json.loads(path.read_text()) if path.exists() else {}


def script_path(feature: str) -> Path:
    return testware() / feature / f"test_{feature}.py"


def file_hashes(feature: str) -> dict:
    files = [testware() / feature / "cases.json", script_path(feature)]
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}


def packet_path(exec_id: str) -> Path:
    return home() / "exec" / exec_id / "packet.json"


def packet_ok(exec_id: str) -> bool:
    try:
        packet = json.loads(packet_path(exec_id).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return bool(packet.get("cases")) and all(c.get("steps") for c in packet["cases"])


def ledger_records(feature=None, case=None) -> list[dict]:
    path = home() / "ledger.jsonl"
    if not path.exists():
        return []
    recs = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return [r for r in recs if (feature is None or r["feature"] == feature) and (case is None or r["case"] == case)]


def current(feature: str, case: str):
    recs = ledger_records(feature, case)
    return recs[-1] if recs else None


def last_approval(feature: str, case: str):
    return next((r for r in reversed(ledger_records(feature, case)) if r["to"] == "approved"), None)


def transition(feature, case, to, runs=(), review=None, note=None):
    """Append one state transition. Refuses any stage stamp that has no evidence behind it."""
    cur = current(feature, case)
    frm = cur["to"] if cur else None
    runs = list(runs)
    if to != "authored" and not (runs and all(packet_ok(r) for r in runs)):
        raise LedgerError(f"{case}: '{to}' needs execution packets on disk, got {runs}")
    hashes = file_hashes(feature)
    if to == "approved":
        if frm != "verified":
            raise LedgerError(f"{case}: approve from '{frm}'")
        if len(runs) != 2 or len(set(runs)) != 2:
            raise LedgerError(f"{case}: approval needs two distinct runs, got {runs}")
        if not review or review.get("result") != "agreed":
            raise LedgerError(f"{case}: approval needs an agreed blind review")
    if to in ("stable", "drift"):
        if frm not in ("approved", "stable", "drift") or cur["hashes"] != hashes:
            raise LedgerError(f"{case}: replay needs an approval whose files are unchanged")
    rec = {"ts": time.time(), "feature": feature, "case": case, "from": frm, "to": to, "runs": runs,
           "hashes": hashes, "review": review, "note": note, "by": "loop"}
    with locked(home() / "ledger.lock"):
        with open(home() / "ledger.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
