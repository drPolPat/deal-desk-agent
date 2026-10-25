"""The MCP server over the real protocol, in-memory (mcp.client.Client connected to the server)."""

import json
from datetime import date, timedelta

import anyio
import pytest
from mcp.client.client import Client

from deal_desk import mcp_server
from deal_desk.audit import JsonlAuditStore
from deal_desk.config import Settings
from deal_desk.tools import MCP_TOOLS
from tests.test_agent import ACTIVITIES, FakeSF
from tests.test_guardrails import OPP_ID

TASK = {"opportunity_id": OPP_ID, "subject": "Call Ibrahim",
        "description": "Confirm the timeline for the proposal.",
        "due_date": (date.today() + timedelta(days=3)).isoformat()}


@pytest.fixture
def env(tmp_path, monkeypatch):
    sf, store = FakeSF(), JsonlAuditStore(tmp_path)
    settings = Settings(_env_file=None, dry_run=False, mcp_allow_writes=False)
    monkeypatch.setattr(mcp_server, "get_sf", lambda: sf)
    monkeypatch.setattr(mcp_server, "get_store", lambda: store)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    return sf, store, settings


def session(*calls):
    """Run tool calls in one in-memory MCP session; return (tools, results)."""
    async def go():
        async with Client(mcp_server.server) as client:
            tools = (await client.list_tools()).tools
            return tools, [await client.call_tool(name, args) for name, args in calls]
    return anyio.run(go)


def text(result):
    return " ".join(c.text for c in result.content if c.type == "text")


def test_lists_the_shared_read_tools_and_hides_create_task_by_default(env):
    tools, _ = session()
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"search_opportunities", "get_opportunity_details", "get_activity_history"}
    shared = {t["name"]: t for t in MCP_TOOLS}
    for name, tool in by_name.items():  # served verbatim from tools.py, not redefined
        assert tool.description == shared[name]["description"]
        assert tool.input_schema == shared[name]["input_schema"]
        assert tool.annotations.read_only_hint is True


def test_each_read_tool_returns_salesforce_data(env):
    _, (search, details, activity) = session(
        ("search_opportunities", {"limit": 5}),
        ("get_opportunity_details", {"opportunity_id": OPP_ID}),
        ("get_activity_history", {"opportunity_id": OPP_ID}),
    )
    assert not search.is_error and json.loads(text(search))[0]["Id"] == OPP_ID
    assert not details.is_error and json.loads(text(details))["Id"] == OPP_ID
    assert not activity.is_error and json.loads(text(activity)) == ACTIVITIES


def test_arguments_are_validated_against_the_shared_schema(env):
    _, (extra, bad_limit) = session(("get_opportunity_details", {"opportunity_id": OPP_ID, "x": 1}),
                                    ("search_opportunities", {"limit": 0}))
    assert extra.is_error and "Invalid arguments" in text(extra)
    assert bad_limit.is_error and "Invalid arguments" in text(bad_limit)


def test_create_task_is_refused_when_disabled(env):
    sf, _, _ = env
    _, (result,) = session(("create_task", TASK))
    assert result.is_error and "disabled" in text(result)
    assert sf.tasks == []


def test_create_task_when_enabled_respects_dry_run(env, tmp_path):
    sf, _, settings = env
    settings.mcp_allow_writes, settings.dry_run = True, True
    tools, (result,) = session(("create_task", TASK))
    assert "create_task" in {t.name for t in tools}
    assert not result.is_error and json.loads(text(result))["created"] is False
    assert sf.tasks == []
    assert json.loads((tmp_path / "tool_writes.jsonl").read_text())["dry_run"] is True


def test_create_task_when_enabled_uses_the_shared_guardrails(env):
    sf, _, settings = env
    settings.mcp_allow_writes = True
    _, (first, duplicate, past) = session(("create_task", TASK), ("create_task", TASK),
                                          ("create_task", {**TASK, "due_date": "2020-01-01"}))
    assert json.loads(text(first))["task_id"] == "00TNEW"
    assert duplicate.is_error and "already has an open" in text(duplicate)
    assert past.is_error and "due_date must be between" in text(past)
    assert len(sf.tasks) == 1  # the duplicate and the past date wrote nothing
