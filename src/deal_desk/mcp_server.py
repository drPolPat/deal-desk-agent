"""MCP server exposing the agent's Salesforce tools to Claude Desktop and Claude Code (stdio).

- Tool names, descriptions and schemas come from tools.py, the same definitions the agent uses.
  Every call is validated against that schema before anything reaches Salesforce.
- Read-only by default: create_task is hidden and refused unless MCP_ALLOW_WRITES=true.
- When enabled, create_task goes through runner.guarded_create_task, the same write path as
  the agent and the review queue: idempotency check, DRY_RUN, "[Deal Desk Agent]" prefix.
  Each call is audited in the tool_writes table.
- Logging goes to stderr only, because stdout carries the MCP protocol.
"""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

import anyio
import jsonschema
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from simple_salesforce.exceptions import SalesforceResourceNotFound

from deal_desk.audit import AuditStore, make_store
from deal_desk.config import Settings, get_settings
from deal_desk.runner import AlreadyActioned, guarded_create_task
from deal_desk.salesforce import ApiLimitExceeded, InvalidInput, SalesforceClient, SalesforceUnavailable
from deal_desk.tools import MCP_TOOLS

TOOLS = {t["name"]: t for t in MCP_TOOLS}
READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                        open_world_hint=True)


class Refused(Exception):
    """A call the server declines; the message goes back to the client as a tool error."""


@lru_cache
def get_sf() -> SalesforceClient:
    return SalesforceClient.from_settings(get_settings())


@lru_cache
def get_store() -> AuditStore:
    return make_store(get_settings())


def visible_tools(settings: Settings) -> list[Tool]:
    return [Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"],
                 annotations=WRITE if t["name"] == "create_task" else READ)
            for t in MCP_TOOLS if t["name"] != "create_task" or settings.mcp_allow_writes]


def create_task(sf: SalesforceClient, settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    if not settings.mcp_allow_writes:
        raise Refused("create_task is disabled on this server (read-only). Set MCP_ALLOW_WRITES=true "
                      "to enable it.")
    today = date.today()
    try:
        due = date.fromisoformat(args["due_date"])
    except ValueError as exc:
        raise Refused("due_date must be an ISO date, e.g. 2026-11-03") from exc
    if not today <= due <= today + timedelta(days=90):
        raise Refused(f"due_date must be between {today} and {today + timedelta(days=90)}")
    try:
        task_id = guarded_create_task(sf, settings, args["opportunity_id"], subject=args["subject"],
                                      description=args["description"], due_date=due)
    except AlreadyActioned as exc:
        raise Refused(f"{exc}; not creating a duplicate") from exc
    get_store().append("tool_writes", {
        "write_id": uuid.uuid4().hex, "ts": datetime.now(UTC).isoformat(), "source": "mcp",
        "opportunity_id": args["opportunity_id"], "task_id": task_id, "subject": args["subject"],
        "due_date": due.isoformat(), "dry_run": settings.dry_run,
    })
    if settings.dry_run:
        return {"dry_run": True, "created": False, "would_create": {
            "opportunity_id": args["opportunity_id"], "subject": args["subject"],
            "due_date": due.isoformat()}}
    return {"dry_run": False, "created": True, "task_id": task_id,
            "opportunity_id": args["opportunity_id"]}


def dispatch(name: str, args: dict[str, Any]) -> Any:
    """Validate against the shared schema, then run the tool. Raises Refused on any rejection."""
    if name not in TOOLS:
        raise Refused(f"Unknown tool: {name}")
    settings = get_settings()
    if name == "create_task" and not settings.mcp_allow_writes:  # before validation: a clear reason
        return create_task(get_sf(), settings, args)
    try:
        jsonschema.Draft202012Validator(TOOLS[name]["input_schema"]).validate(args)
    except jsonschema.ValidationError as exc:
        raise Refused(f"Invalid arguments: {exc.message}") from exc
    sf = get_sf()
    try:
        if name == "search_opportunities":
            return sf.search_opportunities(stage=args.get("stage"), min_amount=args.get("min_amount"),
                                           limit=args.get("limit", 25))
        if name == "get_opportunity_details":
            return sf.get_opportunity_details(args["opportunity_id"])
        if name == "get_activity_history":
            return sf.get_activity_history(args["opportunity_id"])
        return create_task(sf, settings, args)
    except (InvalidInput, SalesforceResourceNotFound) as exc:
        raise Refused(f"Invalid request: {exc}") from exc
    except (ApiLimitExceeded, SalesforceUnavailable) as exc:
        raise Refused(f"Salesforce unavailable: {exc}") from exc


async def on_list_tools(ctx: Any, params: Any) -> ListToolsResult:
    return ListToolsResult(tools=visible_tools(get_settings()))


async def on_call_tool(ctx: Any, params: Any) -> CallToolResult:
    try:
        # Salesforce calls are blocking; keep them off the event loop.
        result = await anyio.to_thread.run_sync(dispatch, params.name, params.arguments or {})
    except Refused as exc:
        return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, default=str))])


server = Server(
    "deal-desk",
    instructions="Salesforce pipeline tools: search open opportunities, read one opportunity and its "
    "activity. create_task (when enabled) writes one open follow-up Task and never edits or deletes.",
    on_list_tools=on_list_tools,
    on_call_tool=on_call_tool,
)


def main() -> None:
    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    main()
