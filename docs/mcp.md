# Using the Salesforce tools from Claude (MCP)

`deal-desk-mcp` is a stdio MCP server on the official Python `mcp` SDK (2.3.0). Its tools are served
from [`src/deal_desk/tools.py`](../src/deal_desk/tools.py), the same names, descriptions and schemas
the agent uses, and every call is validated against those schemas first.

| Tool | Kind | Notes |
|---|---|---|
| `search_opportunities` | read | open deals, oldest activity first; optional `stage`, `min_amount`, `limit` |
| `get_opportunity_details` | read | stage, amount, dates, next step, description, account, owner, contact roles |
| `get_activity_history` | read | tasks and events, newest first |
| `create_task` | write | **off by default**; one open Task, due within 90 days |

## Guardrails (shared with the agent)

- **Read-only by default.** With `MCP_ALLOW_WRITES` unset or false, `create_task` isn't listed, and
  calling it returns a "disabled" error.
- **One write path.** With writes enabled, `create_task` calls `runner.guarded_create_task`, the same
  function the agent's runner and the review queue use. It checks for an open `[Deal Desk Agent]`
  Task (refusing duplicates), honours `DRY_RUN`, and writes through the least-privilege
  `SalesforceClient`, which adds the subject prefix and has no edit or delete method.
- **Audited.** Every `create_task` call is recorded in the `tool_writes` table: JSONL locally, or
  BigQuery when `GCP_PROJECT` and `BQ_DATASET` are set.

The server reads credentials from the repo's `.env`, so it must start in the repository folder
(`uv --directory`, below).

## Claude Desktop (Windows)

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "deal-desk": {
      "command": "C:\\Users\\YOU\\AppData\\Roaming\\Python\\Python311\\Scripts\\uv.exe",
      "args": ["--directory", "C:\\path\\to\\deal-desk-agent", "run", "deal-desk-mcp"]
    }
  }
}
```

Use the full path from `where uv` for `command`. Restart Claude Desktop, then try "Which open deals
haven't had activity in 30 days?". To enable writes, add `"env": {"MCP_ALLOW_WRITES": "true"}`.

## Claude Code

The repo ships a project-scoped [`.mcp.json`](../.mcp.json). Open the repository in Claude Code and
approve the `deal-desk` server; it needs `uv` on your PATH. Or add it from anywhere:

```bash
claude mcp add deal-desk --scope user -- uv --directory /path/to/deal-desk-agent run deal-desk-mcp
```
