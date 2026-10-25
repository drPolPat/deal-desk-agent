"""One run: search open deals -> assess each -> route -> write (or queue) -> audit."""

import logging
import statistics
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import anthropic
from simple_salesforce.exceptions import SalesforceError

from deal_desk.agent import AgentResult, assess
from deal_desk.audit import AuditStore
from deal_desk.config import Settings
from deal_desk.costs import cost_usd
from deal_desk.guardrails import Decision, route
from deal_desk.salesforce import ApiLimitExceeded, SalesforceClient, SalesforceUnavailable
from deal_desk.schemas import DealAssessment

log = logging.getLogger(__name__)
LOG_FIELDS = ("run_id", "opportunity_id", "decision", "status", "confidence", "reasons", "cost_usd",
              "latency_ms", "task_id", "dry_run")


def short_subject(text: str, limit: int = 120) -> str:
    """Fit the next step into a Task subject without cutting a word in half."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:.-") + "…"


def task_fields(a: DealAssessment, today: date, run_id: str, prompt_version: str) -> dict[str, Any]:
    email = a.follow_up_email
    lines = ["Why this deal is at risk:", *[f"- {r}" for r in a.risk_reasons], "",
             f"Recommended next step: {a.next_step}"]
    if email:
        lines += ["", f"Draft email to {email.to_contact_name}:", f"Subject: {email.subject}", "", email.body]
    lines += ["", f"(Deal Desk Agent run {run_id}, prompt {prompt_version}, confidence {a.confidence:.2f})"]
    return {"subject": short_subject(a.next_step), "description": "\n".join(lines),
            "due_date": today + timedelta(days=a.task_due_in_days)}


class AlreadyActioned(Exception):
    """This app already left an open Task on the opportunity."""


def guarded_create_task(sf: SalesforceClient, settings: Settings, opportunity_id: str, *,
                        subject: str, description: str, due_date: date) -> str | None:
    """The one write path, shared by the runner, the review queue and the MCP server.

    Idempotency check, then DRY_RUN, then SalesforceClient.create_task (which adds the
    "[Deal Desk Agent]" prefix and is never retried). Returns the Task Id, or None in dry run.
    """
    if sf.has_open_agent_task(opportunity_id):
        raise AlreadyActioned("this opportunity already has an open Deal Desk Agent task")
    if settings.dry_run:
        return None
    return sf.create_task(opportunity_id, subject=subject, description=description, due_date=due_date)


def decide(result: AgentResult, settings: Settings, today: date) -> Decision:
    return route(result.assessment, result.error, result.opportunity, result.activities,
                 today=today, confidence_threshold=settings.confidence_threshold,
                 auto_write_max_amount=settings.auto_write_max_amount, stale_days=settings.stale_days)


def audit_row(run_id: str, result: AgentResult, decision: Decision, settings: Settings,
              task_id: str | None) -> dict[str, Any]:
    opp, a = result.opportunity, result.assessment
    return {
        "run_id": run_id,
        "ts": datetime.now(UTC).isoformat(),
        "opportunity_id": result.opportunity_id,
        "opportunity_name": opp.get("Name"),
        "amount": opp.get("Amount"),
        "input_record_ids": [opp.get("Id"), opp.get("AccountId"), *[x["Id"] for x in result.activities]],
        "model": result.model or None,  # None: no model call was made
        "prompt_version": settings.prompt_version,
        "effort": settings.effort,
        "tokens": dict(result.usage),
        "cost_usd": cost_usd(result.model, result.usage) if result.model else 0.0,
        "latency_ms": result.latency_ms,
        "tool_calls": result.tool_calls,
        "validation_attempts": result.validation_attempts,
        "status": a.status if a else None,
        "confidence": a.confidence if a else None,
        "decision": decision.action,
        "reasons": decision.reasons,
        "rule_flags": decision.flags,
        "assessment": a.model_dump() if a else None,
        "error": result.error,
        "dry_run": settings.dry_run,
        "task_id": task_id,
        "review_status": "pending" if decision.action == "human_review" else None,
        "approved_by": "guardrails" if task_id else None,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = sorted(r["latency_ms"] for r in rows if r["model"])  # skipped deals would skew p50
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    actions = ("auto_write", "human_review", "no_action", "already_actioned")
    by_action = {a: sum(r["decision"] == a for r in rows) for a in actions}
    return {
        "opportunities": len(rows),
        **by_action,
        "tokens": {k: sum(r["tokens"].get(k, 0) for r in rows)
                   for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                             "output_tokens")},
        "cost_usd": round(sum(costs), 4) if len(costs) == len(rows) else None,
        "latency_p50_ms": round(statistics.median(latencies)) if latencies else None,
        "latency_p95_ms": latencies[min(len(latencies) - 1, round(0.95 * (len(latencies) - 1)))]
        if latencies else None,
    }


def run(settings: Settings, sf: SalesforceClient, llm: anthropic.Anthropic, store: AuditStore,
        *, limit: int = 50, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    run_id = uuid.uuid4().hex[:12]
    started = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    stopped = None
    try:
        for opp in sf.search_opportunities(limit=limit):
            try:
                # Idempotency, checked before the model call so re-runs cost nothing for deals the
                # agent already actioned. The deal comes back once the owner completes that Task.
                # Checked in dry run too, so a dry run predicts exactly what --write would do.
                if sf.has_open_agent_task(opp["Id"]):
                    result = AgentResult(opp["Id"], None, None, opp, [], model="")
                    decision = Decision("already_actioned", ["open Deal Desk Agent task already exists"])
                else:
                    result = assess(llm, sf, opp["Id"], settings, today)
                    decision = decide(result, settings, today)
            except SalesforceError as exc:  # e.g. record deleted mid-run: skip it, keep going
                log.warning(f"  skip {opp['Id']}: {type(exc).__name__}",
                            extra={"fields": {"event": "skip", "run_id": run_id,
                                              "opportunity_id": opp["Id"]}})
                continue
            task_id = None
            if decision.action == "auto_write":
                try:
                    fields = task_fields(result.assessment, today, run_id, settings.prompt_version)
                    task_id = guarded_create_task(sf, settings, opp["Id"], **fields)
                except AlreadyActioned:  # another writer (review queue, MCP) got there since the pre-check
                    decision = Decision("already_actioned", ["open Deal Desk Agent task already exists"],
                                        decision.flags)
                except SalesforceUnavailable as exc:  # not retried (see create_task): a human decides
                    decision = Decision("human_review", [f"task write failed: {exc}"], decision.flags)
            row = audit_row(run_id, result, decision, settings, task_id)
            store.append("decisions", row)
            rows.append(row)
            log.info(f"  {decision.action:<13} {row['status'] or '-':<8} conf={row['confidence'] or 0:.2f} "
                     f"{row['latency_ms']:>6}ms  {opp['Name']}"
                     + (f"\n                {'; '.join(decision.reasons)}" if decision.reasons else ""),
                     extra={"fields": {"event": "decision", **{k: row[k] for k in LOG_FIELDS}}})
    except (ApiLimitExceeded, SalesforceUnavailable) as exc:
        stopped = f"{type(exc).__name__}: {exc}"  # stop cleanly; everything so far is audited
        log.error(f"run stopped early: {stopped}",
                  extra={"fields": {"event": "run_stopped", "run_id": run_id}})
    summary = {"run_id": run_id, "started": started.isoformat(), "model": settings.model,
               "prompt_version": settings.prompt_version, "dry_run": settings.dry_run,
               "stopped_early": stopped, "sf_api_usage": sf.api_usage(), **summarize(rows)}
    store.append("runs", summary)
    log.info("run finished", extra={"fields": {"event": "run_summary", **summary}})
    return summary
