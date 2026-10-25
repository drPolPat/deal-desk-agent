"""Tool definitions given to the model. Read-only on purpose: the agent never gets a write tool;
its only write (create_task) is made by routing code after the guardrails pass or a human approves."""

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
