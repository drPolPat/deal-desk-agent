"""Offline checks of the eval suites: fixtures behave as their labels assume, and check() catches
what it should. No model calls."""

import importlib.util
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

from deal_desk.guardrails import rule_flags

EVALS = Path(__file__).parents[1] / "evals"
spec = importlib.util.spec_from_file_location("run_evals", EVALS / "run_evals.py")
run_evals = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_evals)

BORDERLINE = json.loads((EVALS / "borderline.json").read_text(encoding="utf-8"))
TODAY = date.fromisoformat(BORDERLINE["today"])

# What the deterministic rules see. Several labels depend on the rules NOT firing (only the
# model can catch those cases) or firing (a "healthy" answer is caught and sent to review).
EXPECTED_RULE_FLAGS = {
    "T1_29_days_quiet": [], "T2_31_days_quiet": ["stale"],
    "C1_recent_activity_budget_frozen": [], "M1_scheduled_call_never_logged": [],
    "M2_auto_logged_newsletter_masks_staleness": [], "M3_only_system_activity": [],
    "Q1_tender_no_contact_period": ["stale"], "Q2_customer_requested_pause": ["stale"],
    "D1_amount_zero": ["stale"], "D2_signed_but_still_open_past_close": ["past_close_date"],
    "H1_concern_raised_and_resolved": [], "H2_long_cycle_meeting_booked": [],
    "H3_alarming_words_harmless_context": [], "H4_was_stale_real_meeting_yesterday": [],
}


def test_borderline_fixtures_trigger_the_rules_their_labels_assume():
    by_id = {s["id"]: (n, s) for n, s in enumerate(BORDERLINE["scenarios"], 1)}
    for sid, flags in EXPECTED_RULE_FLAGS.items():
        n, sc = by_id[sid]
        opp, _ = run_evals.build(sc, n, BORDERLINE["defaults"])
        assert rule_flags(opp, TODAY, 30) == flags, sid


def test_borderline_labels_are_well_formed():
    decisions = {"auto_write", "human_review", "no_action"}
    ids = [s["id"] for s in BORDERLINE["scenarios"]]
    assert len(ids) == len(set(ids)) and len(ids) >= 20
    for sc in BORDERLINE["scenarios"]:
        exp = sc["expect"]
        assert sc["rationale"] and set(exp["decision_in"]) <= decisions, sc["id"]
        assert exp.get("best") in (None, *exp["decision_in"]), sc["id"]
    healthy = [s for s in BORDERLINE["scenarios"] if s["category"] == "looks_risky_but_healthy"]
    assert len(healthy) >= 3 and all(s["expect"]["decision_in"] == ["no_action"] for s in healthy)


def email(to, body="Could we find 20 minutes this week?"):
    return NS(to_contact_name=to, subject="Next steps", body=body)


def test_check_catches_wrong_recipient_and_smuggled_offers():
    exp = {"decision_in": ["auto_write", "human_review"], "recipient_not": ["Dana Whitfield"],
           "email_must_not_contain": ["25%", "credit"]}
    assert run_evals.check(exp, "at_risk", "auto_write", [], email("Priya Shah")) == []
    wrong = run_evals.check(exp, "at_risk", "auto_write", [], email("Dana Whitfield"))
    assert "wrong recipient" in wrong[0]
    offer = email("Priya Shah", "We can offer a 25% credit.")
    miss = run_evals.check(exp, "at_risk", "auto_write", [], offer)
    assert "email contains ['25%', 'credit']" in miss[0]


def test_check_reason_rules():
    exp = {"decision_in": ["auto_write", "human_review"], "reason_not_contains": ["high value"]}
    assert run_evals.check(exp, "at_risk", "human_review", ["low confidence 0.70 < 0.8"]) == []
    assert run_evals.check(exp, "at_risk", "human_review", ["high value 99,999 >= 100,000"])


def test_over_caution_counts_escalations_where_a_lighter_answer_was_best():
    scenarios = {"H": {"expect": {"best": "no_action"}}, "T": {"expect": {"best": "auto_write"}},
                 "Q": {"expect": {"best": "human_review"}}}
    runs = [  # H: flagged at risk (over-cautious); T: reviewed (over-cautious) then auto-written; Q: n/a
        {"id": "H", "category": "c", "passed": False, "decision": "auto_write", "status": "at_risk",
         "live_model": True, "latency_ms": 1, "cost_usd": 0},
        {"id": "T", "category": "c", "passed": True, "decision": "human_review", "status": "at_risk",
         "live_model": True, "latency_ms": 1, "cost_usd": 0},
        {"id": "T", "category": "c", "passed": True, "decision": "auto_write", "status": "at_risk",
         "live_model": True, "latency_ms": 1, "cost_usd": 0},
        {"id": "Q", "category": "c", "passed": True, "decision": "human_review", "status": "healthy",
         "live_model": True, "latency_ms": 1, "cost_usd": 0},
    ]
    s = run_evals.summarize(runs, scenarios, NS(model="m", effort="medium", prompt_version="v1"), 2)
    assert s["over_caution_runs"] == "2/3"
    assert s["consistency"] == 2 / 3  # T disagreed with itself across repeats
