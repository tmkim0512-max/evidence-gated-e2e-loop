"""Minimal blind verification: the author's verdict never reaches the reviewers.

Two reviewer processes see only an observation packet, never each other's answers.
The loop assigns reviewer ids. Disagreement stays unconfirmed until a third party adjudicates.
(The generator != verifier discipline is a practice learned from a previous team; see README.)
"""
import json
import subprocess
import sys
from pathlib import Path

from .evidence import Verdict

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REVIEWERS = [[sys.executable, str(REPO / "reviewers" / "rules.py"), "--policy", p] for p in ("lenient", "strict")]
STEP_FIELDS = ("key", "expect", "observed", "screenshot", "blocked_signal")
SPAWNED = 0


def observation_packet(case_packet: dict) -> dict:
    """Allowlist copy: anything else in the run output (exit codes, author notes) is dropped."""
    return {"case": case_packet["case"], "steps": [{k: s.get(k) for k in STEP_FIELDS} for s in case_packet["steps"]]}


def ask(cmd: list[str], packet: dict, timeout: int = 60):
    global SPAWNED
    SPAWNED += 1
    try:
        p = subprocess.run(cmd, input=json.dumps(packet), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if p.returncode != 0:
        return None, f"exit {p.returncode}"
    try:
        return json.loads(p.stdout), None
    except json.JSONDecodeError:
        return None, "unparseable output"


def parse(packet: dict, raw: dict, reviewer_id: str):
    keys = {s["key"] for s in packet["steps"]}
    out, problems = {}, []
    for v in raw.get("verdicts", []):
        key, verdict = v.get("key"), v.get("verdict")
        if key not in keys or key in out or verdict not in Verdict.__members__:
            problems.append(f"{reviewer_id}: rejected answer {key}={verdict}")
            continue
        out[key] = {"reviewer": reviewer_id, "verdict": verdict, "evidence": v.get("evidence") or {}, "reason": v.get("reason")}
    problems += [f"{reviewer_id}: no answer for {k}" for k in sorted(keys - out.keys())]
    return out, problems


def gate(answer: dict, step: dict) -> dict:
    """PASS needs a quote found in the observation; BLOCKED needs a machine signal. FAIL is never downgraded."""
    quote = answer["evidence"].get("quote")
    if answer["verdict"] == Verdict.PASS:
        cited = bool(quote) and quote.upper() != "PASS" and quote in (step.get("observed") or "")
        if not cited:
            return {**answer, "verdict": Verdict.INCONCLUSIVE, "reason": "no_evidence", "downgraded": True}
    if answer["verdict"] == Verdict.BLOCKED and not step.get("blocked_signal"):
        return {**answer, "verdict": Verdict.INCONCLUSIVE, "reason": "unsignaled_blocked", "downgraded": True}
    return answer


def merge(answers: list[dict]) -> dict:
    reviewers = sorted({a["reviewer"] for a in answers})
    if len(answers) != 2 or len(reviewers) != 2:  # one reviewer, or the same reviewer twice
        return {"verdict": Verdict.INCONCLUSIVE, "confirmed": False, "reason": "insufficient_sample", "reviewers": reviewers}
    verdicts = {a["verdict"] for a in answers}
    if len(verdicts) == 1:
        return {"verdict": verdicts.pop(), "confirmed": True, "reviewers": reviewers}
    return {"verdict": Verdict.INCONCLUSIVE, "confirmed": False, "reason": "verifier_disagree", "reviewers": reviewers}


def adjudicate(merged: dict, adjudicator: str, verdict: str, reason: str) -> dict:
    if merged.get("reason") != "verifier_disagree":
        raise ValueError("only a two-reviewer disagreement can be adjudicated")
    if adjudicator in merged["reviewers"]:
        raise ValueError("adjudicator must not be one of the reviewers")
    if not reason:
        raise ValueError("adjudication needs a reason")
    return {"verdict": verdict, "confirmed": True, "adjudicated_by": adjudicator, "reason": reason,
            "reviewers": merged["reviewers"]}


def review_case(case_packet: dict, reviewers=None) -> dict:
    packet = observation_packet(case_packet)
    steps = {s["key"]: s for s in packet["steps"]}
    answers, problems, downgraded = {k: [] for k in steps}, [], 0
    ids = []
    for i, cmd in enumerate(reviewers or DEFAULT_REVIEWERS, 1):
        rid = f"rv{i}"
        ids.append(rid)
        raw, err = ask(cmd, packet)
        if err:
            problems.append(f"{rid}: {err}")
            continue
        parsed, probs = parse(packet, raw, rid)
        problems += probs
        for key, answer in parsed.items():
            gated = gate(answer, steps[key])
            downgraded += bool(gated.get("downgraded"))
            answers[key].append(gated)
    merged = {k: merge(a) for k, a in answers.items()}
    disagreed = sum(m.get("reason") == "verifier_disagree" for m in merged.values())
    all_pass = all(m["confirmed"] and m["verdict"] == Verdict.PASS for m in merged.values())
    return {"reviewers": ids, "result": "agreed" if all_pass else ("disagree" if disagreed else "rejected"),
            "steps": len(merged), "agreed": sum(m["confirmed"] for m in merged.values()), "disagreed": disagreed,
            "downgraded": downgraded, "problems": problems, "merged": merged}
