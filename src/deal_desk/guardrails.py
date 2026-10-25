"""Guardrails: every check that stands between the model's output and a Salesforce write.

The order of trust is: Salesforce data > deterministic rules in this file > the model.
The model proposes; this module decides whether that proposal may be written automatically,
must go to a human, or needs no action.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import ValidationError

from deal_desk.schemas import DealAssessment

# --- 1. Output validation ----------------------------------------------------------------


def validate_output(text: str) -> tuple[DealAssessment | None, str | None]:
    """Parse and validate the model's JSON. Returns (assessment, None) or (None, error message).

    The error message is fed back to the model for one retry (see agent.py).
    """
    try:
        return DealAssessment.model_validate_json(text), None
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'output'}: {e['msg']}" for e in exc.errors())
        return None, errors[:2000]


# --- 2. Deterministic deal rules (independent of the model) -------------------------------


def rule_flags(opportunity: dict[str, Any], today: date, stale_days: int) -> list[str]:
    """Facts computed in code from Salesforce fields. Used to cross-check the model."""
    flags = []
    last = opportunity.get("LastActivityDate")
    if last is None:
        flags.append("no_activity")
    elif (today - date.fromisoformat(last)).days >= stale_days:
        flags.append("stale")
    if opportunity.get("CloseDate") and date.fromisoformat(opportunity["CloseDate"]) < today:
        flags.append("past_close_date")
    if not opportunity.get("OpportunityContactRoles"):
        flags.append("no_contacts")
    if opportunity.get("Amount") is None:
        flags.append("missing_amount")
    return flags


RISK_FLAGS = {"no_activity", "stale", "past_close_date"}

# Instruction-like text inside Salesforce free-text fields. The prompt already tells the model to
# treat these fields as data; this is a second, deterministic line of defence.
INJECTION_RE = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|system (note|prompt)|"
    r"you are now|set (the )?confidence|disregard (the )?(rules|instructions)",
    re.IGNORECASE,
)


def injection_suspected(opportunity: dict[str, Any], activities: list[dict[str, Any]]) -> bool:
    texts = [opportunity.get("Description"), opportunity.get("NextStep")]
    texts += [a.get(k) for a in activities for k in ("Subject", "Description")]
    return any(t and INJECTION_RE.search(t) for t in texts)


# --- 3. Grounding: cited evidence must exist in the Salesforce data -----------------------

_SEGMENT_RE = re.compile(r"^(\w+)((?:\[\d+\])*)$")


def resolve_path(data: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Resolve 'opportunity.OpportunityContactRoles[0].Contact.Name'. Returns (found, value)."""
    node: Any = data
    for segment in path.split("."):
        m = _SEGMENT_RE.match(segment)
        if not m or not isinstance(node, dict) or m.group(1) not in node:
            return False, None
        node = node[m.group(1)]
        for idx in re.findall(r"\[(\d+)\]", m.group(2)):
            if not isinstance(node, list) or int(idx) >= len(node):
                return False, None
            node = node[int(idx)]
    return True, node


def _same_value(actual: Any, cited: str) -> bool:
    cited = cited.strip()
    if actual is None:
        return cited.lower() in ("null", "none", "")
    if isinstance(actual, bool):
        return cited.lower() == str(actual).lower()
    if isinstance(actual, int | float):
        try:
            return float(cited.replace(",", "")) == float(actual)
        except ValueError:
            return False
    return str(actual).strip() == cited


def grounding_failures(assessment: DealAssessment, data: dict[str, Any]) -> list[str]:
    """Each evidence item must name a real field AND quote its real value."""
    failures = []
    for ev in assessment.evidence:
        found, actual = resolve_path(data, ev.field)
        if not found:
            failures.append(f"nonexistent field: {ev.field}")
        elif not _same_value(actual, ev.value):
            failures.append(f"wrong value for {ev.field}: cited {ev.value!r}, actual {actual!r}")
    return failures


# --- 4. Content policy on what would be written -------------------------------------------

COMMITMENT_RE = re.compile(
    r"\b\d{1,2}\s?% (off|discount)|\bdiscount|\bfree (of charge|months?)|\bprice (cut|reduction)",
    re.IGNORECASE,
)


def policy_failures(assessment: DealAssessment, opportunity: dict[str, Any]) -> list[str]:
    failures = []
    if assessment.opportunity_id != opportunity["Id"]:
        failures.append(f"assessment is for {assessment.opportunity_id}, not {opportunity['Id']}")
    email = assessment.follow_up_email
    if email:
        contacts = {r["Contact"]["Name"] for r in opportunity.get("OpportunityContactRoles") or []
                    if r.get("Contact")}
        if email.to_contact_name and email.to_contact_name not in contacts:
            failures.append(f"email recipient {email.to_contact_name!r} is not a contact on this deal")
        if COMMITMENT_RE.search(f"{email.subject} {email.body}"):
            failures.append("email offers a discount or pricing commitment")
    return failures


# --- 5. Routing ---------------------------------------------------------------------------

Action = Literal["auto_write", "human_review", "no_action"]


@dataclass
class Decision:
    action: Action
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def route(
    assessment: DealAssessment | None,
    error: str | None,
    opportunity: dict[str, Any],
    activities: list[dict[str, Any]],
    *,
    today: date,
    confidence_threshold: float,
    auto_write_max_amount: float,
    stale_days: int,
) -> Decision:
    """auto_write only if confidence >= threshold AND amount < limit AND every check passes.

    Anything uncertain goes to a human. Healthy deals propose no write, so write-specific checks
    (amount, recipient) only apply to at-risk deals; trust checks apply to every output.
    """
    flags = rule_flags(opportunity, today, stale_days)
    if assessment is None:
        return Decision("human_review", [f"no valid model output: {error}"], flags)

    data = {"opportunity": opportunity, "activities": activities}
    reasons = [f"ungrounded: {f}" for f in grounding_failures(assessment, data)]
    reasons += [f"policy: {f}" for f in policy_failures(assessment, opportunity)]
    if assessment.confidence < confidence_threshold:
        reasons.append(f"low confidence {assessment.confidence:.2f} < {confidence_threshold}")
    if injection_suspected(opportunity, activities):
        reasons.append("possible prompt injection in Salesforce text fields")
    if assessment.status == "healthy" and RISK_FLAGS & set(flags):
        reasons.append(f"model says healthy but rules flag {sorted(RISK_FLAGS & set(flags))}")

    if assessment.status == "at_risk":
        amount = opportunity.get("Amount")
        if amount is None:
            reasons.append("amount unknown, cannot apply the auto-write limit")
        elif amount >= auto_write_max_amount:
            reasons.append(f"high value {amount:,.0f} >= {auto_write_max_amount:,.0f}")
        if assessment.follow_up_email and not assessment.follow_up_email.to_contact_name:
            reasons.append("no contact to send the follow-up to")

    if reasons:
        return Decision("human_review", reasons, flags)
    return Decision("auto_write" if assessment.status == "at_risk" else "no_action", [], flags)
