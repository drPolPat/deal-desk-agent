"""One run: search open deals -> assess each -> route -> write (or queue) -> audit."""

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


def task_fields(a: DealAssessment, today: date, run_id: str, prompt_version: str) -> dict[str, Any]:
    email = a.follow_up_email
    lines = ["Why this deal is at risk:", *[f"- {r}" for r in a.risk_reasons], "",
             f"Recommended next step: {a.next_step}"]
    if email:
        lines += ["", f"Draft email to {email.to_contact_name}:", f"Subject: {email.subject}", "", email.body]
    lines += ["", f"(Deal Desk Agent run {run_id}, prompt {prompt_version}, confidence {a.confidence:.2f})"]
    return {"subject": a.next_step[:120], "description": "\n".join(lines),
            "due_date": today + timedelta(days=a.task_due_in_days)}


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
        "model": result.model,
        "prompt_version": settings.prompt_version,
        "effort": settings.effort,
        "tokens": dict(result.usage),
        "cost_usd": cost_usd(result.model, result.usage),
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
    latencies = sorted(r["latency_ms"] for r in rows)
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
                result = assess(llm, sf, opp["Id"], settings, today)
            except SalesforceError as exc:  # e.g. record deleted mid-run: skip it, keep going
                print(f"  skip {opp['Id']}: {type(exc).__name__}")
                continue
            decision = decide(result, settings, today)
            # Idempotency: re-runs must not stack duplicate follow-ups on the same deal.
            # Checked in dry run too, so a dry run predicts exactly what --write would do.
            if decision.action == "auto_write" and sf.has_open_agent_task(opp["Id"]):
                decision = Decision("already_actioned", ["open Deal Desk Agent task already exists"],
                                    decision.flags)
            task_id = None
            if decision.action == "auto_write" and not settings.dry_run:
                try:
                    fields = task_fields(result.assessment, today, run_id, settings.prompt_version)
                    task_id = sf.create_task(opp["Id"], **fields)
                except SalesforceUnavailable as exc:  # not retried (see create_task): a human decides
                    decision = Decision("human_review", [f"task write failed: {exc}"], decision.flags)
            row = audit_row(run_id, result, decision, settings, task_id)
            store.append("decisions", row)
            rows.append(row)
            print(f"  {decision.action:<13} {row['status'] or '-':<8} conf={row['confidence'] or 0:.2f} "
                  f"{row['latency_ms']:>6}ms  {opp['Name']}"
                  + (f"\n                {'; '.join(decision.reasons)}" if decision.reasons else ""))
    except (ApiLimitExceeded, SalesforceUnavailable) as exc:
        stopped = f"{type(exc).__name__}: {exc}"  # stop cleanly; everything so far is audited
    summary = {"run_id": run_id, "started": started.isoformat(), "model": settings.model,
               "prompt_version": settings.prompt_version, "dry_run": settings.dry_run,
               "stopped_early": stopped, "sf_api_usage": sf.api_usage(), **summarize(rows)}
    store.append("runs", summary)
    return summary
