"""Evidence harvester - pure read. Outcomes are recomputed from files, never taken from workers.

A queue entry marked 'done' or a worker printing SUCCESS proves nothing; only ledger records
whose packets exist and whose file hashes still match count.
"""
import json
from collections import Counter
from pathlib import Path

from . import evidence as ev
from .evidence import Code, Outcome, Owner


class DenominatorError(Exception):
    pass


def case_status(feature: str, case: str, hashes_now: dict):
    """(ledger state, evidence problem or None) for one test case."""
    rec = ev.current(feature, case)
    if rec is None:
        return "none", None
    if rec["to"] in ("approved", "stable"):
        appr = ev.last_approval(feature, case)
        if appr is None or rec["hashes"] != hashes_now or len(set(appr["runs"])) != 2:
            return rec["to"], Code.EVIDENCE_MISMATCH
        if not all(ev.packet_ok(r) for r in appr["runs"] + rec["runs"]):
            return rec["to"], Code.EVIDENCE_LOST
    return rec["to"], None


def item_outcome(feature: str, qitem: dict):
    state, fail = qitem["state"], qitem.get("failure") or {}
    if state == "pending":
        return Outcome.NOT_STARTED, {}
    if state == "in_progress":
        return Outcome.RUNNING, {}
    if state == "failed":
        if fail.get("code") == Code.PAUSED_BUDGET:
            return Outcome.WAITING_DECISION, {"failure": fail}
        if fail.get("code") == Code.EXTERNAL_BLOCK:  # re-check, don't trust the stored code
            outcome, record = ev.classify_block(fail.get("reason"), fail.get("owner"))
            return outcome, {"failure": record}
        return Outcome.FAILED, {"failure": fail}
    # done: recompute from evidence
    hashes_now = ev.file_hashes(feature)
    excs = ev.load_exceptions(feature)
    ids = [c["id"] for c in ev.load_cases(feature)["cases"]]
    status = {c: case_status(feature, c, hashes_now) for c in ids}
    detail = {"cases": {c: s for c, (s, _) in status.items()}, "exceptions": sorted(excs)}
    problems = {c: p for c, (_, p) in status.items() if p}
    if problems:
        return Outcome.FAILED, {**detail, "failure": ev.failure(next(iter(problems.values())), Owner.LOOP, str(problems))}
    runnable = [c for c in ids if c not in excs]
    approved = [c for c in runnable if status[c][0] in ("approved", "stable")]
    if any((ev.current(feature, c)["review"] or {}).get("result") == "disagree" for c in runnable if status[c][0] == "verified"):
        return Outcome.WAITING_DECISION, detail
    if approved and len(approved) == len(runnable) and not excs:
        return Outcome.SUCCESS, detail
    if approved:
        return Outcome.PARTIAL, detail
    return Outcome.FAILED, {**detail, "failure": ev.failure(Code.NO_APPROVAL, Owner.LOOP, "no case approved")}


def harvest(run_dir: Path) -> dict:
    manifest = ev.read_manifest(run_dir / "manifest.txt")
    queue = {it["item"]: it for it in json.loads((run_dir / "queue.json").read_text())["items"]}
    if sorted(queue) != sorted(manifest):
        raise DenominatorError(f"queue {sorted(queue)} != manifest {sorted(manifest)}")
    items = {f: item_outcome(f, queue[f]) for f in manifest}
    counts = Counter({o.value: 0 for o in Outcome})
    counts.update(o.value for o, _ in items.values())
    if sum(counts.values()) != len(manifest):
        raise DenominatorError(f"outcomes sum {sum(counts.values())} != denominator {len(manifest)}")
    cases = Counter()
    review = Counter()
    for f, (o, d) in items.items():
        for c, st in d.get("cases", {}).items():
            cases["total"] += 1
            cases["exception"] += c in d["exceptions"]
            cases["approved"] += st in ("approved", "stable") and c not in d["exceptions"]
            rv = (ev.current(f, c) or {}).get("review") or {}
            for k in ("steps", "agreed", "disagreed", "downgraded"):
                review[k] += rv.get(k, 0)
        if o in (Outcome.BLOCKED_EXTERNAL, Outcome.FAILED, Outcome.WAITING_DECISION):
            owner = (d.get("failure") or {}).get("owner")
            cases["external_items" if o == Outcome.BLOCKED_EXTERNAL else "loop_items" if owner == Owner.LOOP else "other_items"] += 1
            if o == Outcome.BLOCKED_EXTERNAL:
                cases["external"] += len(ev.load_cases(f)["cases"])
    return {"run_id": run_dir.name, "denominator": len(manifest), "counts": dict(counts),
            "items": {f: {"outcome": o.value, **d} for f, (o, d) in items.items()},
            "cases": dict(cases), "review": dict(review)}


def check(run_dir: Path) -> dict:
    rep = harvest(run_dir)
    ev.atomic_write_json(run_dir / "report.json", rep)
    return rep


def describe(f: str, item: dict) -> str:
    if item.get("failure"):
        return f"{f}: {item['failure']['code']} ({item['failure']['owner']})"
    cases = item.get("cases", {})
    if not cases:
        return f
    ok = sum(s in ("approved", "stable") for c, s in cases.items() if c not in item["exceptions"])
    text = f"{f}: {ok}/{len(cases) - len(item['exceptions'])} approved"
    if item["exceptions"]:
        text += f", {', '.join(item['exceptions'])} known-defect exception (not counted as success)"
    return text


def to_markdown(rep: dict) -> str:
    n, counts, cs, rv = rep["denominator"], rep["counts"], rep["cases"], rep["review"]
    lines = [f"run {rep['run_id']}  items={n}  (manifest {n})", "", "| outcome | count | items |", "|---|---:|---|"]
    for o in Outcome:
        names = [describe(f, it) for f, it in rep["items"].items() if it["outcome"] == o]
        lines.append(f"| {o} | {counts[o]} | {'; '.join(names)} |")
    total = sum(counts.values())
    lines.append(f"| **sum = denominator** | {total} = {n} | {'OK' if total == n else 'MISMATCH'} |")
    runnable = cs.get("total", 0) - cs.get("external", 0)
    lines += ["", f"cases: {cs.get('approved', 0)} approved / {cs.get('total', 0)} total · "
              f"{cs.get('exception', 0)} known-defect exception · {cs.get('external', 0)} externally blocked · "
              f"loop-failed items {cs.get('loop_items', 0)} · runnable approval rate "
              f"{cs.get('approved', 0)}/{runnable} (A/(N-E))",
              f"review: {rv.get('steps', 0)} step verdicts · {rv.get('agreed', 0)} agreed · "
              f"{rv.get('disagreed', 0)} disagreed · {rv.get('downgraded', 0)} unsupported PASS/BLOCKED downgraded"]
    return "\n".join(lines)
