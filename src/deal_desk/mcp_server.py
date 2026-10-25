"""MCP server exposing the same Salesforce tools to Claude Desktop and Claude Code (stdio).

The guardrails still apply here: the same least-privilege SalesforceClient (no update or delete),
create_task refuses duplicates and honours DRY_RUN, and every create_task call is audited.
Logging goes to stderr only, because stdout carries the MCP protocol.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from simple_salesforce.exceptions import SalesforceResourceNotFound

from deal_desk.audit import AuditStore, make_store
from deal_desk.config import get_settings
from deal_desk.salesforce import ApiLimitExceeded, InvalidInput, SalesforceClient, SalesforceUnavailable

server = MCPServer(
    "deal-desk",
    instructions="Salesforce pipeline tools. Reads are safe. create_task writes one open follow-up Task "
    "on an opportunity; it never edits or deletes records and refuses if this app already left an "
    "open Task there.",
)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                        open_world_hint=True)
OppId = Annotated[str, Field(description="Salesforce Opportunity Id (starts with 006)")]


@lru_cache
def get_sf() -> SalesforceClient:
    return SalesforceClient.from_settings(get_settings())


@lru_cache
def get_store() -> AuditStore:
    return make_store(get_settings())


def _safe[T](fn: Any) -> T:
    """Turn Salesforce errors into tool errors the client shows to the model and the user."""
    try:
        return fn()
    except (InvalidInput, SalesforceResourceNotFound) as exc:
        raise ToolError(f"Invalid request: {exc}") from exc
    except (ApiLimitExceeded, SalesforceUnavailable) as exc:
        raise ToolError(f"Salesforce unavailable: {exc}") from exc


@server.tool(annotations=READ)
def search_opportunities(
    stage: Annotated[str | None, Field(description="Exact StageName, e.g. 'Proposal/Price Quote'")] = None,
    min_amount: Annotated[float | None, Field(ge=0)] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
) -> list[dict[str, Any]]:
    """List open opportunities, oldest activity first (never-touched deals first)."""
    return _safe(lambda: get_sf().search_opportunities(stage=stage, min_amount=min_amount, limit=limit))


@server.tool(annotations=READ)
def get_opportunity_details(opportunity_id: OppId) -> dict[str, Any]:
    """One opportunity: stage, amount, close date, next step, description, last activity date,
    account, owner and contact roles."""
    return _safe(lambda: get_sf().get_opportunity_details(opportunity_id))


@server.tool(annotations=READ)
def get_activity_history(opportunity_id: OppId,
                         limit: Annotated[int, Field(ge=1, le=50)] = 20) -> list[dict[str, Any]]:
    """Tasks and events on an opportunity, newest first."""
    return _safe(lambda: get_sf().get_activity_history(opportunity_id, limit))


@server.tool(annotations=WRITE)
def create_task(
    opportunity_id: OppId,
    subject: Annotated[str, Field(min_length=3, max_length=200)],
    description: Annotated[str, Field(min_length=10, max_length=5000)],
    due_date: Annotated[date, Field(description="ISO date, today or within the next 90 days")],
) -> dict[str, Any]:
    """Create one open follow-up Task on an opportunity, prefixed '[Deal Desk Agent]'.

    Refuses if this app already has an open Task on the opportunity. With DRY_RUN=true it only
    reports what it would create.
    """
    settings, today = get_settings(), date.today()
    if not today <= due_date <= today + timedelta(days=90):
        raise ToolError(f"due_date must be between {today} and {today + timedelta(days=90)}")
    sf = get_sf()
    if _safe(lambda: sf.has_open_agent_task(opportunity_id)):
        raise ToolError("This opportunity already has an open Deal Desk Agent task; "
                        "not creating a duplicate.")
    task_id = None
    if not settings.dry_run:
        task_id = _safe(lambda: sf.create_task(opportunity_id, subject=subject, description=description,
                                               due_date=due_date))
    get_store().append("tool_writes", {
        "write_id": uuid.uuid4().hex, "ts": datetime.now(UTC).isoformat(), "source": "mcp",
        "opportunity_id": opportunity_id, "task_id": task_id, "subject": subject,
        "due_date": due_date.isoformat(), "dry_run": settings.dry_run,
    })
    if settings.dry_run:
        return {"dry_run": True, "created": False, "would_create": {
            "opportunity_id": opportunity_id, "subject": subject, "due_date": due_date.isoformat()}}
    return {"dry_run": False, "created": True, "task_id": task_id, "opportunity_id": opportunity_id}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
