"""Deterministic reviewer: observation packet on stdin -> verdicts on stdout.

--policy lenient  PASS when the expected text is in the observation.
--policy strict   additionally requires a non-empty screenshot file, else INCONCLUSIVE.
Swap in any command (an LLM CLI, a human form) that honours the same stdin/stdout contract.
"""
import argparse
import json
import os
import sys


def judge(step: dict, strict: bool) -> dict:
    observed, expect = step.get("observed"), step["expect"]
    if step.get("blocked_signal"):
        return {"verdict": "BLOCKED", "evidence": {"type": "signal", "quote": step["blocked_signal"]}}
    if observed is None:
        return {"verdict": "INCONCLUSIVE", "evidence": {}, "reason": "evidence_missing"}
    shot = step.get("screenshot")
    if strict and not (shot and os.path.exists(shot) and os.path.getsize(shot) > 0):
        return {"verdict": "INCONCLUSIVE", "evidence": {}, "reason": "screenshot_missing"}
    if expect in observed:
        return {"verdict": "PASS", "evidence": {"type": "observed", "quote": expect}}
    return {"verdict": "FAIL", "evidence": {"type": "observed", "quote": observed}, "reason": f"expected {expect!r}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", choices=["lenient", "strict"], default="lenient")
    strict = ap.parse_args().policy == "strict"
    packet = json.load(sys.stdin)
    verdicts = [{"key": s["key"], **judge(s, strict)} for s in packet["steps"]]
    json.dump({"verdicts": verdicts}, sys.stdout)


if __name__ == "__main__":
    main()
