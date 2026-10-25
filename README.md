# Deal Desk Agent

An AI agent that reviews open Salesforce opportunities, flags stale or at-risk deals, and drafts a
next step and follow-up email for each. Low-risk, high-confidence results are written back to
Salesforce as a Task. Anything low-confidence, high-value or failing a check goes to a human review
queue. Nothing reaches Salesforce without passing every guardrail or a human approving it.

> Work in progress. Milestones 1–3 (Salesforce integration, agent with guardrails and routing, evals
> and CI) are done; deployment, MCP server and review UI are next. The full write-up comes with M4.

## Model comparison

Same 21 eval scenarios, prompt `v1`, effort `medium`, run 2026-10-25. 16 scenarios call the model;
the other 5 replay canned bad outputs to test the guardrails deterministically, so they cost nothing.
Prices verified against Anthropic's pricing page on the same day.

| Category | Opus 5.5 | Sonnet 5.5 (default) | Haiku 5.5 |
|---|---|---|---|
| Stale deals flagged | 5/5 | 5/5 | 5/5 |
| Healthy deals not flagged | 5/5 | 5/5 | 5/5 |
| High-value deals routed to a human | 3/3 | 3/3 | 3/3 |
| Ungrounded outputs rejected | 4/4 | 4/4 | 4/4 |
| Malformed or missing data | 3/3 | 3/3 | 3/3 |
| Prompt injection | 1/1 | 1/1 | 1/1 |
| **Overall pass rate** | **100%** | **100%** | **100%** |
| Cost per eval run | $0.41 | $0.11¹ | $0.011 |
| Cost per deal reviewed | $0.025 | $0.007¹ | $0.0007 |
| Latency p50 / p95 per deal | 9.6 s / 11.3 s | 5.9 s / 6.7 s | 5.9 s / 7.6 s |

¹ Measured with a warm prompt cache (no cache writes). From a cold cache, the Sonnet run costs about $0.17.

**Reading this:** the suite doesn't separate the three models. Its scenarios are deliberately
clear-cut, so it works as a regression gate (CI fails below 90% overall or 80% in any category), not
as a benchmark. On this evidence Haiku 5.5 gives the same routing decisions at about a tenth of
Sonnet's cost. Sonnet 5.5 stays the default because the suite doesn't yet measure what would justify
paying more: email quality, the wording of risk reasons, and confidence calibration on ambiguous deals.
Adding graded ambiguous scenarios is the next step for the eval set. Switch models with `MODEL=...`.

Re-run it: `uv run python evals/run_evals.py --model claude-haiku-5-5`. Results are saved in
[evals/results/](evals/results/).
