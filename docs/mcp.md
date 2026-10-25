# Using the Salesforce tools from Claude (MCP)

`deal-desk-mcp` is a stdio MCP server built on the official Python `mcp` SDK (v2). It exposes the
same tools the agent uses, plus the one write:

| Tool | Kind | Notes |
|---|---|---|
| `search_opportunities` | read | open deals, oldest activity first; optional `stage`, `min_amount`, `limit` |
| `get_opportunity_details` | read | stage, amount, dates, next step, description, account, owner, contact roles |
| `get_activity_history` | read | tasks and events, newest first |
| `create_task` | write | one open Task prefixed `[Deal Desk Agent]`; due date within 90 days |

Read tools are annotated read-only, so clients can allow them without prompting. `create_task`
carries the same guardrails as the agent:

- It runs through the least-privilege `SalesforceClient`, which has no update or delete method.
- It refuses if this app already has an open Task on the opportunity.
- With `DRY_RUN=true` it reports what it would create and writes nothing.
- Every call is audited in the `tool_writes` table (JSONL locally, BigQuery when configured).

The server reads credentials from the repo's `.env`, so it must start in the repository directory.

## Claude Code

The repo ships a project-scoped [`.mcp.json`](../.mcp.json). Open the repository in Claude Code,
approve the `deal-desk` server when asked, and the tools are available as `mcp__deal-desk__*`.
It needs `uv` on your PATH.

To add it from anywhere instead (user scope; `--directory` makes `.env` resolve):

```bash
claude mcp add deal-desk --scope user -- uv --directory /path/to/deal-desk-agent run deal-desk-mcp
```

## Claude Desktop

Settings > Developer > Edit Config, then add (adjust the path):

```json
{
  "mcpServers": {
    "deal-desk": {
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\deal-desk-agent", "run", "deal-desk-mcp"]
    }
  }
}
```

Restart Claude Desktop. Try: "Which open deals haven't had activity in 30 days? Draft a follow-up
for the most valuable one." Claude will ask before running `create_task`.
