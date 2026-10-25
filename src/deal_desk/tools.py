"""The one set of Salesforce tool definitions (name, description, JSON schema).

The agent gets READ_TOOLS only: it never holds a write tool, and its writes are made by routing
code after the guardrails pass or a human approves. The MCP server serves MCP_TOOLS, these same
definitions, with create_task behind an opt-in flag.
"""

from typing import Any

_OPP_ID = {"type": "string", "description": "Salesforce Opportunity Id (starts with 006)"}

READ_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_opportunity_details",
        "description": "Get one opportunity: stage, amount, close date, next step, description, "
        "last activity date, account, owner and contact roles.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"opportunity_id": _OPP_ID},
            "required": ["opportunity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_activity_history",
        "description": "Get the most recent tasks and events on an opportunity, newest first.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"opportunity_id": _OPP_ID},
            "required": ["opportunity_id"],
            "additionalProperties": False,
        },
    },
]

SEARCH_TOOL: dict[str, Any] = {
    "name": "search_opportunities",
    "description": "List open opportunities, oldest activity first (never-touched deals first). "
    "Optional filters: exact stage name and minimum amount.",
    "input_schema": {
        "type": "object",
        "properties": {
            "stage": {"type": "string", "description": "Exact StageName, e.g. 'Proposal/Price Quote'"},
            "min_amount": {"type": "number", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
        },
        "additionalProperties": False,
    },
}

CREATE_TASK_TOOL: dict[str, Any] = {
    "name": "create_task",
    "description": "Create one open follow-up Task on an opportunity, prefixed '[Deal Desk Agent]'. "
    "The only write this app can make: it never edits or deletes records, refuses if this app "
    "already has an open Task on the opportunity, and writes nothing in dry run.",
    "input_schema": {
        "type": "object",
        "properties": {
            "opportunity_id": _OPP_ID,
            "subject": {"type": "string", "minLength": 3, "maxLength": 200},
            "description": {"type": "string", "minLength": 10, "maxLength": 5000},
            "due_date": {"type": "string", "format": "date",
                         "description": "ISO date, today or within the next 90 days"},
        },
        "required": ["opportunity_id", "subject", "description", "due_date"],
        "additionalProperties": False,
    },
}

MCP_TOOLS: list[dict[str, Any]] = [SEARCH_TOOL, *READ_TOOLS, CREATE_TASK_TOOL]
