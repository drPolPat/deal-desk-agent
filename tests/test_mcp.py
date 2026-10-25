"""The MCP server over the real protocol, in-process (mcp.client.Client connected to the server)."""

import json
from datetime import date, timedelta

import anyio
import pytest
from mcp.client.client import Client

from deal_desk import mcp_server
from deal_desk.audit import JsonlAuditStore
from deal_desk.config import Settings
from tests.test_agent import FakeSF
from tests.test_guardrails import OPP_ID


@pytest.fixture
def env(tmp_path, monkeypatch):
    sf, store = FakeSF(), JsonlAuditStore(tmp_path)
    settings = Settings(_env_file=None, dry_run=False)
    monkeypatch.setattr(mcp_server, "get_sf", lambda: sf)
    monkeypatch.setattr(mcp_server, "get_store", lambda: store)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    return sf, store, settings


def call(name, args=None, list_tools=False):
    async def go():
        async with Client(mcp_server.server) as client:
            if list_tools:
                return (await client.list_tools()).tools
            return await client.call_tool(name, args or {})
    return anyio.run(go)


def text(result):
    return " ".join(c.text for c in result.content if c.type == "text")


def test_exposes_four_tools_and_marks_reads_read_only(env):
    tools = {t.name: t for t in call(None, list_tools=True)}
    assert set(tools) == {"search_opportunities", "get_opportunity_details", "get_activity_history",
                          "create_task"}
    for name in ("search_opportunities", "get_opportunity_details", "get_activity_history"):
        assert tools[name].annotations.read_only_hint is True
    assert tools["create_task"].annotations.read_only_hint is False
    assert tools["create_task"].annotations.destructive_hint is False


def test_read_tools_return_salesforce_data(env):
    result = call("get_opportunity_details", {"opportunity_id": OPP_ID})
    assert not result.is_error and json.loads(text(result))["Id"] == OPP_ID
    result = call("search_opportunities", {"limit": 5})
    assert not result.is_error and OPP_ID in text(result)


def test_create_task_writes_once_and_is_audited(env, tmp_path):
    sf, _, _ = env
    args = {"opportunity_id": OPP_ID, "subject": "Call Ibrahim",
            "description": "Confirm the timeline for the proposal.",
            "due_date": (date.today() + timedelta(days=3)).isoformat()}
    first = call("create_task", args)
    assert not first.is_error and json.loads(text(first))["task_id"] == "00TNEW"
    second = call("create_task", args)
    assert second.is_error and "already has an open" in text(second)
    assert len(sf.tasks) == 1
    audit = (tmp_path / "tool_writes.jsonl").read_text().splitlines()
    assert len(audit) == 1 and json.loads(audit[0])["source"] == "mcp"


def test_create_task_honours_dry_run(env):
    sf, _, settings = env
    settings.dry_run = True
    result = call("create_task", {"opportunity_id": OPP_ID, "subject": "Call Ibrahim",
                                  "description": "Confirm the timeline for the proposal.",
                                  "due_date": date.today().isoformat()})
    assert json.loads(text(result))["created"] is False and sf.tasks == []


@pytest.mark.parametrize("args, message", [
    ({"due_date": "2020-01-01"}, "due_date must be between"),
    ({"subject": "x"}, "subject"),
])
def test_create_task_validates_its_inputs(env, args, message):
    base = {"opportunity_id": OPP_ID, "subject": "Call Ibrahim",
            "description": "Confirm the timeline for the proposal.", "due_date": date.today().isoformat()}
    result = call("create_task", {**base, **args})
    assert result.is_error and message in text(result)
    assert env[0].tasks == []
