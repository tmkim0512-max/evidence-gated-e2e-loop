"""Minimal blind verification: gating, merge, adjudication, reviewer isolation."""
import pytest

from loop import verify
from loop.evidence import Verdict

STEP = {"key": "CART-0001#2", "expect": "Total: 30,000", "observed": "Total: 30,000",
        "screenshot": None, "blocked_signal": None}


def answer(verdict, quote=None, reviewer="rv1"):
    return {"reviewer": reviewer, "verdict": verdict, "evidence": {"quote": quote} if quote else {}, "reason": None}


@pytest.mark.parametrize("quote", [None, "", "PASS", "Total: 99,999"])
def test_pass_without_real_evidence_is_downgraded(quote):
    assert verify.gate(answer("PASS", quote), STEP)["verdict"] == Verdict.INCONCLUSIVE


def test_cited_pass_and_uncited_fail_are_kept():
    assert verify.gate(answer("PASS", "Total: 30,000"), STEP)["verdict"] == Verdict.PASS
    assert verify.gate(answer("FAIL"), STEP)["verdict"] == Verdict.FAIL  # a defect must not quietly vanish


def test_blocked_needs_a_machine_signal():
    assert verify.gate(answer("BLOCKED"), STEP)["verdict"] == Verdict.INCONCLUSIVE
    assert verify.gate(answer("BLOCKED"), {**STEP, "blocked_signal": "app_unreachable"})["verdict"] == Verdict.BLOCKED


def test_merge():
    agree = verify.merge([answer("PASS", reviewer="rv1"), answer("PASS", reviewer="rv2")])
    assert agree["confirmed"] and agree["verdict"] == Verdict.PASS
    split = verify.merge([answer("PASS", reviewer="rv1"), answer("FAIL", reviewer="rv2")])
    assert split == {**split, "verdict": Verdict.INCONCLUSIVE, "confirmed": False, "reason": "verifier_disagree"}
    for answers in ([answer("PASS", reviewer="rv1")] * 2, [answer("PASS")]):
        assert verify.merge(answers)["reason"] == "insufficient_sample"


def test_adjudication_rules():
    split = verify.merge([answer("PASS", reviewer="rv1"), answer("FAIL", reviewer="rv2")])
    with pytest.raises(ValueError, match="must not be one of the reviewers"):
        verify.adjudicate(split, "rv1", "PASS", "looked again")
    with pytest.raises(ValueError, match="reason"):
        verify.adjudicate(split, "rv3", "FAIL", "")
    short = verify.merge([answer("PASS", reviewer="rv1")])
    with pytest.raises(ValueError, match="disagreement"):
        verify.adjudicate(short, "rv3", "PASS", "fill the gap")
    assert verify.adjudicate(split, "rv3", "FAIL", "total is wrong on screenshot")["confirmed"]


def test_parse_rejects_unknown_keys_records_missing_and_ignores_self_declared_ids():
    packet = {"case": "C", "steps": [{"key": "C#1"}, {"key": "C#2"}]}
    raw = {"reviewer_id": "boss", "verdicts": [{"key": "C#1", "verdict": "PASS"}, {"key": "X#9", "verdict": "PASS"}]}
    out, problems = verify.parse(packet, raw, "rv2")
    assert list(out) == ["C#1"] and out["C#1"]["reviewer"] == "rv2"
    assert any("X#9" in p for p in problems) and any("no answer for C#2" in p for p in problems)


def test_reviewers_never_see_author_fields():
    case = {"case": "C", "author_verdict": "PASS", "steps": [{**STEP, "pytest_rc": 0, "author_says": "ok"}]}
    assert verify.observation_packet(case) == {"case": "C", "steps": [STEP]}


def test_two_real_reviewer_processes_disagree_without_screenshot():
    review = verify.review_case({"case": "CART-0001", "steps": [STEP]})  # lenient PASS, strict INCONCLUSIVE
    assert review["reviewers"] == ["rv1", "rv2"]
    assert review["result"] == "disagree" and review["disagreed"] == 1
