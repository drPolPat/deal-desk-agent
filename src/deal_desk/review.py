"""Human review queue: list pending items, approve (writes the Task) or reject.

A human approval is the only way an item that failed a guardrail can reach Salesforce.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from deal_desk.audit import AuditStore
from deal_desk.config import Settings
from deal_desk.runner import task_fields
from deal_desk.salesforce import SalesforceClient, SalesforceUnavailable
from deal_desk.schemas import DealAssessment


class ReviewError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def pending(store: AuditStore) -> list[dict[str, Any]]:
    """Latest decision per deal, if it is human_review and nobody has reviewed it yet."""
    reviewed = {(r["run_id"], r["opportunity_id"]) for r in store.read("reviews")}
    latest: dict[str, dict[str, Any]] = {}
    for d in store.read("decisions"):  # oldest first, so a newer run supersedes older items
        latest[d["opportunity_id"]] = d
    return [d for d in latest.values()
            if d["decision"] == "human_review" and (d["run_id"], d["opportunity_id"]) not in reviewed]


def _find(store: AuditStore, run_id: str, opportunity_id: str) -> dict[str, Any]:
    for item in pending(store):
        if item["run_id"] == run_id and item["opportunity_id"] == opportunity_id:
            return item
    raise ReviewError(404, "no pending review item for this run and opportunity")


def _record(store: AuditStore, settings: Settings, item: dict[str, Any], action: str,
            **extra: Any) -> dict[str, Any]:
    row = {"review_id": uuid.uuid4().hex, "ts": datetime.now(UTC).isoformat(), "run_id": item["run_id"],
           "opportunity_id": item["opportunity_id"], "action": action, "reviewer": settings.reviewer_name,
           "dry_run": settings.dry_run, "task_id": None, **extra}
    store.append("reviews", row)
    return row


def approve(store: AuditStore, sf: SalesforceClient, settings: Settings, run_id: str,
            opportunity_id: str, *, subject: str | None = None, description: str | None = None,
            note: str | None = None, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    item = _find(store, run_id, opportunity_id)
    if sf.has_open_agent_task(opportunity_id):
        raise ReviewError(409, "this opportunity already has an open Deal Desk Agent task")

    raw = item.get("assessment")
    assessment = DealAssessment.model_validate(raw) if raw else None
    if subject or description:
        if not (subject and subject.strip() and description and description.strip()):
            raise ReviewError(422, "an edited task needs both subject and description")
        days = assessment.task_due_in_days if assessment else 3
        fields = {"subject": subject.strip()[:200], "description": description.strip()[:30000],
                  "due_date": today + timedelta(days=days)}
    elif assessment and assessment.status == "at_risk":
        fields = task_fields(assessment, today, run_id, settings.prompt_version)
    elif assessment:
        fields = None  # approving a "healthy" assessment confirms that no follow-up is needed
    else:
        raise ReviewError(422, "the model produced no valid output; provide subject and description")

    task_id = None
    if fields and not settings.dry_run:
        fields["description"] += f"\n\nApproved by {settings.reviewer_name} in the review queue."
        try:
            task_id = sf.create_task(opportunity_id, **fields)
        except SalesforceUnavailable as exc:  # nothing recorded: the item stays pending
            raise ReviewError(502, f"Salesforce write failed, item is still pending: {exc}") from exc
    return _record(store, settings, item, "approve", task_id=task_id, note=note,
                   edited=bool(subject or description), task_subject=fields["subject"] if fields else None)


def reject(store: AuditStore, settings: Settings, run_id: str, opportunity_id: str,
           *, note: str | None = None) -> dict[str, Any]:
    return _record(store, settings, _find(store, run_id, opportunity_id), "reject", note=note)
