"""Per-opportunity agent loop: read tools -> structured output -> validation, one retry."""

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

import anthropic
from anthropic.lib._parse._transform import transform_schema
from simple_salesforce.exceptions import SalesforceResourceNotFound

from deal_desk.config import Settings
from deal_desk.guardrails import validate_output
from deal_desk.salesforce import InvalidInput, SalesforceClient
from deal_desk.schemas import DealAssessment
from deal_desk.tools import READ_TOOLS

OUTPUT_SCHEMA = transform_schema(DealAssessment)
TOOL_NAMES = {t["name"] for t in READ_TOOLS}


def make_llm(settings: Settings) -> anthropic.Anthropic:
    """SDK client with a request timeout; the SDK retries 429/5xx/connection errors with backoff."""
    return anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value() or None,
                               timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries)


@cache
def system_prompt(version: str) -> str:
    return (Path(__file__).parent / "prompts" / f"{version}.md").read_text(encoding="utf-8")


@dataclass
class AgentResult:
    opportunity_id: str
    assessment: DealAssessment | None
    error: str | None
    opportunity: dict[str, Any]
    activities: list[dict[str, Any]]
    model: str
    usage: Counter = field(default_factory=Counter)
    tool_calls: int = 0
    validation_attempts: int = 0
    latency_ms: int = 0


def assess(
    llm: anthropic.Anthropic, sf: SalesforceClient, opportunity_id: str, settings: Settings, today: date
) -> AgentResult:
    started = time.perf_counter()
    fetched: dict[str, Any] = {}

    def fetch(name: str) -> Any:
        # One Salesforce call per tool per opportunity, shared by the model and the routing code.
        if name not in fetched:
            get = sf.get_opportunity_details if name == "get_opportunity_details" else sf.get_activity_history
            fetched[name] = get(opportunity_id)
        return fetched[name]

    def run_tool(block: Any) -> dict[str, Any]:
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id}
        if block.name not in TOOL_NAMES:
            return {**result, "is_error": True, "content": f"Unknown tool {block.name}"}
        if block.input.get("opportunity_id") != opportunity_id:
            return {**result, "is_error": True,
                    "content": f"Out of scope: you may only read opportunity {opportunity_id}"}
        try:
            return {**result, "content": json.dumps(fetch(block.name), default=str)}
        except (InvalidInput, SalesforceResourceNotFound) as exc:
            return {**result, "is_error": True, "content": str(exc)[:500]}
        # SalesforceUnavailable / ApiLimitExceeded propagate: they stop the run, not just this deal.

    out = AgentResult(opportunity_id, None, None, {}, [], settings.model)
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": f"Today is {today.isoformat()}. Review opportunity {opportunity_id}.",
    }]
    try:
        for _ in range(settings.max_agent_turns):
            resp = llm.messages.create(
                model=settings.model,
                max_tokens=settings.max_output_tokens,
                # Breakpoint on the shared prefix (tools + system) so every deal after the first reads
                # it from cache; the top-level cache_control below covers turns within one deal.
                system=[{"type": "text", "text": system_prompt(settings.prompt_version),
                         "cache_control": {"type": "ephemeral"}}],
                tools=READ_TOOLS,
                messages=messages,
                output_config={"effort": settings.effort,
                               "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
                cache_control={"type": "ephemeral"},
            )
            out.model = resp.model
            for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                      "cache_read_input_tokens"):
                out.usage[k] += getattr(resp.usage, k, None) or 0
            # Append the whole content unchanged (thinking blocks included): history stays append-only.
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "tool_use":
                results = [run_tool(b) for b in resp.content if b.type == "tool_use"]
                out.tool_calls += len(results)
                messages.append({"role": "user", "content": results})
                continue
            if resp.stop_reason != "end_turn":  # refusal, max_tokens, pause_turn
                out.error = f"model stopped: {resp.stop_reason}"
                break

            out.validation_attempts += 1
            text = "".join(b.text for b in resp.content if b.type == "text")
            out.assessment, out.error = validate_output(text)
            if out.assessment or out.validation_attempts >= 2:
                break
            messages.append({"role": "user", "content":
                             f"Your JSON failed validation: {out.error}\nReturn the corrected JSON only."})
        else:
            out.error = out.error or "agent turn limit reached"
    except anthropic.APIError as exc:  # after the SDK's own retries with backoff
        out.assessment, out.error = None, f"LLM call failed: {type(exc).__name__}: {str(exc)[:300]}"

    # Routing always uses the real Salesforce record, even if the model skipped a tool.
    out.opportunity = fetch("get_opportunity_details")
    out.activities = fetch("get_activity_history")
    out.latency_ms = round((time.perf_counter() - started) * 1000)
    return out
