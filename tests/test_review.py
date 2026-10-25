import pytest

from deal_desk.audit import JsonlAuditStore
from deal_desk.review import ReviewError, approve, pending, reject
from tests.test_agent import SETTINGS, FakeSF
from tests.test_guardrails import OPP_ID, TODAY, at_risk, healthy

LIVE = SETTINGS.model_copy(update={"dry_run": False, "reviewer_name": "Pat Reviewer"})


def queued(store, run_id="r1", opp_id=OPP_ID, decision="human_review", assessment=None):
    a = assessment if assessment is not None else at_risk().model_dump()
    store.append("decisions", {"run_id": run_id, "opportunity_id": opp_id, "decision": decision,
                               "assessment": a or None})


@pytest.fixture
def store(tmp_path):
    return JsonlAuditStore(tmp_path)


def test_pending_shows_latest_unreviewed_items_only(store):
    queued(store, "r1", OPP_ID)
    queued(store, "r1", "006jV000002RKcbQAG")
    queued(store, "r2", "006jV000002RKcbQAG", decision="no_action")  # newer run supersedes it
    assert [(p["run_id"], p["opportunity_id"]) for p in pending(store)] == [("r1", OPP_ID)]
    reject(store, LIVE, "r1", OPP_ID, note="owner is on it")
    assert pending(store) == []


def test_approve_writes_the_drafted_task_and_records_the_reviewer(store):
    queued(store)
    sf = FakeSF()
    row = approve(store, sf, LIVE, "r1", OPP_ID, today=TODAY)
    assert row["task_id"] == "00TNEW" and row["reviewer"] == "Pat Reviewer" and not row["edited"]
    _, fields = sf.tasks[0]
    assert "Draft email to Ibrahim Yusuf" in fields["description"]
    assert "Approved by Pat Reviewer" in fields["description"]
    assert pending(store) == []


def test_approve_with_edits_uses_the_reviewers_text(store):
    queued(store)
    sf = FakeSF()
    approve(store, sf, LIVE, "r1", OPP_ID, subject="Call the CFO", description="Escalate pricing.",
            today=TODAY)
    assert sf.tasks[0][1]["subject"] == "Call the CFO"


def test_dry_run_approval_records_but_does_not_write(store):
    queued(store)
    sf = FakeSF()
    row = approve(store, sf, SETTINGS, "r1", OPP_ID, today=TODAY)
    assert sf.tasks == [] and row["task_id"] is None and row["dry_run"]


def test_approving_a_healthy_assessment_writes_nothing(store):
    queued(store, assessment=healthy().model_dump())
    sf = FakeSF()
    row = approve(store, sf, LIVE, "r1", OPP_ID, today=TODAY)
    assert sf.tasks == [] and row["action"] == "approve" and row["task_subject"] is None


def test_approve_refuses_duplicates_and_unknown_items(store):
    queued(store)
    sf = FakeSF()
    sf.tasks.append((OPP_ID, {}))  # an open agent task already exists
    with pytest.raises(ReviewError) as exc:
        approve(store, sf, LIVE, "r1", OPP_ID, today=TODAY)
    assert exc.value.status == 409
    with pytest.raises(ReviewError) as exc:
        approve(store, FakeSF(), LIVE, "nope", OPP_ID, today=TODAY)
    assert exc.value.status == 404


def test_invalid_model_output_needs_reviewer_text(store):
    queued(store, assessment={})
    with pytest.raises(ReviewError) as exc:
        approve(store, FakeSF(), LIVE, "r1", OPP_ID, today=TODAY)
    assert exc.value.status == 422
    row = approve(store, FakeSF(), LIVE, "r1", OPP_ID, subject="Re-engage", description="Call Ibrahim.",
                  today=TODAY)
    assert row["task_id"] == "00TNEW"
