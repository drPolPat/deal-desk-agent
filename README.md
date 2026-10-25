# Deal Desk Agent

[![ci](https://github.com/drPolPat/deal-desk-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/drPolPat/deal-desk-agent/actions/workflows/ci.yml)

An AI agent that reviews open Salesforce opportunities, flags stale or at-risk deals, and drafts a
next step and a follow-up email for each. Low-risk, high-confidence results are written back to
Salesforce as a Task on the opportunity. Anything low-confidence, high-value or failing a check goes
to a human review queue. **Nothing reaches Salesforce without passing every guardrail or a human
approving it.**

Built with Python 3.12, FastAPI, the Anthropic Python SDK (Claude Sonnet 5.5, tool use and structured
outputs), simple-salesforce, Google Cloud Run, Secret Manager and BigQuery, plus GitHub Actions for CI/CD.

## The problem

Every pipeline review starts the same way: someone scrolls the opportunity list looking for deals
nobody has touched in a month, close dates that have already passed, and deals without a contact.
That is slow, it happens weekly at best, and stale deals quietly inflate the forecast until the
quarter ends.

An LLM can do the first pass in seconds. But letting a model write into the system of record is a
real risk: an invented fact in a CRM note, a follow-up promising a discount, or a duplicate task on
every run all erode trust in the CRM. So this project is mostly about the guardrails around the
model: what it may read, how its output is checked, and when a person has to decide.

## What it does

For each open opportunity, the agent:

1. Reads the opportunity, its contact roles and its activity history through read-only tools.
2. Returns a structured assessment: healthy or at risk, the reasons, a confidence score, the exact
   Salesforce fields its decision rests on, a recommended next step, and a draft email.
3. Code checks that assessment (schema, grounding, rules, policy) and routes it:
   - **auto_write**: an open Task is created on the opportunity with the next step and draft email
   - **human_review**: it lands in the review queue with the reasons it couldn't be auto-written
   - **no_action**: the deal is healthy
   - **already_actioned**: the agent already left an open Task here, so the deal is skipped before
     the model is called

On the seeded demo org (29 open deals): 9 auto-written, 7 sent to review (3 high-value, 2
low-confidence, 1 with no contact to email, 1 prompt-injection attempt), 13 healthy. Every decision
matched the scenario the deal was seeded to test.

A Task the agent wrote to the demo org (synthetic data, email body shortened):

```text
Subject: [Deal Desk Agent] Contact Ibrahim Yusuf this week to confirm the project is still active and the timeline is still valid. Send proposal v2

Why this deal is at risk:
- No completed activity in the last 30 days. The last activity was a discovery call on 2026-07-05, about 112 days ago.
- The deal is in Proposal/Price Quote stage at 75% probability, but the only activity on record is discovery.
  The next step, 'Send proposal v2', has no activity behind it.
- The close date of 2026-11-13 is less than 3 weeks away and there is no recent engagement.

Recommended next step: Contact Ibrahim Yusuf this week to confirm the project is still active and the timeline
is still valid. Send proposal v2 if it is, and log the activity. If the 2026-11-13 close date is no longer
realistic, update it.

Draft email to Ibrahim Yusuf:
Subject: Checking in on Aurora staff scheduling proposal
Hi Ibrahim, It has been a while since our discovery call in July ... Do you have 30 minutes for a call this week or next?

(Deal Desk Agent run 1bc3452a7b73, prompt v1, confidence 0.90)
```

## Architecture

```mermaid
flowchart LR
    subgraph GH[GitHub Actions]
        CI[lint + unit tests] --> EV[eval gate<br/>21 scenarios] --> IMG[container build<br/>+ health check] --> DEP[deploy<br/>Workload Identity, no keys]
    end

    subgraph GCP[Google Cloud]
        SM[(Secret Manager<br/>SF + Anthropic keys,<br/>review token)]
        subgraph CR[Cloud Run: deal-desk-agent, scale to zero]
            API[FastAPI<br/>POST /runs, GET /reviews,<br/>approve / reject] --> RUN[runner]
            RUN --> AG[agent loop]
            AG --> G[guardrails.py<br/>validation, grounding, rules,<br/>policy, routing]
            G --> RQ[review.py]
        end
        BQ[(BigQuery<br/>decisions, runs, reviews<br/>append-only)]
        LOG[Cloud Logging<br/>structured JSON]
    end

    CLAUDE[Claude Sonnet 5.5<br/>read tools only]
    SF[(Salesforce<br/>integration user:<br/>read + create Task)]

    DEP --> CR
    SM -. env at start .-> CR
    AG <-->|tool use + structured output| CLAUDE
    AG -->|read opportunity, activities| SF
    G -->|auto_write only| SF
    RQ -->|on human approval| SF
    RUN --> BQ
    RQ --> BQ
    CR --> LOG
    REV[Reviewer] -->|bearer token| API
```

The model never holds a write tool. The only Salesforce write, `create_task`, is called by routing
code after the guardrails pass, or by the review queue after a human approves.

## Guardrails, and why each exists

All of the decision logic is in one file, [`src/deal_desk/guardrails.py`](src/deal_desk/guardrails.py),
so a reviewer can read every rule in one place.

| # | Guardrail | Why it exists |
|---|---|---|
| 1 | **Read-only, scoped tools.** The model gets `get_opportunity_details` and `get_activity_history`, with strict schemas. A call for any other opportunity ID returns an error. | A model can't misuse a write it doesn't have. Scoping stops a prompt-injected record from steering the agent into reading other deals. |
| 2 | **Structured output + Pydantic validation.** The JSON schema comes from a Pydantic model; ranges, lengths and cross-field rules (an at-risk deal must have reasons and an email) are checked in code. One retry feeds the validation error back; a second failure goes to a human. | The API guarantees the JSON shape, not its meaning. A retry fixes most slips cheaply; anything still broken is a human's call, not a silent drop. |
| 3 | **Grounding check.** Every evidence item must name a real field (`opportunity.LastActivityDate`, `activities[0].ActivityDate`, ...) and quote its real value. | Catches invented fields and wrong numbers before they become a CRM note, which is the hallucination that does the most damage. |
| 4 | **Deterministic rules.** Code computes stale (30+ days), no activity, past close date, no contacts and missing amount. If the model calls a deal healthy while the rules flag it, the deal goes to review. | The model and plain code check each other. Disagreement is a signal, not something to average away. |
| 5 | **Prompt-injection defence.** The prompt treats Salesforce text as data, and a regex flags instruction-like text in descriptions, next steps and activities. | Salesforce free-text fields are user input. The seeded injection asks for confidence 1.0 and a 40% discount; the model ignored it, and the detector sent the deal to a human anyway. |
| 6 | **Content policy.** The email recipient must be a contact on the deal; discounts or pricing commitments are rejected. | The agent recommends; commercial commitments stay with the owner. |
| 7 | **Routing.** `auto_write` only if confidence ≥ 0.8 **and** amount < 100,000 **and** every check passes. Missing amount or no contact means review. | High-value deals deserve human eyes, and uncertainty should cost a reviewer's minute, not a wrong action. Both thresholds are config. |
| 8 | **Idempotency.** Before calling the model, the runner checks for an open Task with the `[Deal Desk Agent]` prefix and skips the deal if one exists. | Re-runs must not stack duplicate follow-ups. Checking first also makes re-runs free for actioned deals. Verified live: the second `--write` run created 0 Tasks. |
| 9 | **Least privilege, three layers.** Salesforce: the integration user reads Account/Contact/Opportunity and creates Tasks. Code: `SalesforceClient` has no update or delete method, and a test fails if one is added. Agent: no write tools. | One layer can be misconfigured; three independent layers make that survivable (see the Salesforce lessons below for why the code layer matters). |
| 10 | **Dry run by default.** Routing and audit run, but nothing is written until `--write` (CLI) or `DRY_RUN=false` (service). | New prompts and models can be trialled against the real org with zero risk. |
| 11 | **Audit trail.** One BigQuery row per decision: input record IDs, model, prompt version, effort, tokens, cost, latency, decision, reasons, rule flags, the full assessment, Task ID, and who approved. Reviews go to their own append-only table. If a BigQuery write fails, the full row goes to Cloud Logging and the run continues. | "Why did the agent write this?" must be answerable months later, and an audit record must never be silently lost. |
| 12 | **Reliability.** Timeouts on every call; exponential backoff with jitter on Salesforce network/5xx errors; the Anthropic SDK's own retries on 429/5xx. A clean stop on `REQUEST_LIMIT_EXCEEDED`, and before this app uses 80% of the org's daily API allowance. `create_task` is never retried. | Other integrations share the org's API limit. Retrying a write after a timeout can duplicate it, so a failed write goes back to a human instead. |
| 13 | **Cost and latency.** Prompt caching on the shared system prompt and tools; a per-run summary with tokens, cost and p50/p95 latency. | The first full run cost $0.41. A cache breakpoint on the shared prefix, plus the corrected Sonnet 5.5 cache-hit price ($0.10/MTok), brought it to about $0.23. |
| 14 | **Access control on the service.** Every endpoint except `/health` needs a bearer token from Secret Manager, and the service refuses everything if no token is configured. | Fail closed. Reviewer identity is a configured name for now; IAP is the production answer (see next steps). |

## Evals

[`evals/scenarios.json`](evals/scenarios.json) holds 21 labeled scenarios with a fixed "today", so
results don't drift with the calendar. No Salesforce is needed: a fixture client serves the same read
tools, and every scenario goes through the real agent loop and the real routing code.

Five scenarios replay **canned bad model outputs** (an invented `ChurnRisk__c` field, an
out-of-range `activities[4]`, a wrong date, an invented nested field, non-JSON twice). A model
can't be relied on to misbehave on demand, so these test the guardrails deterministically, for free.

CI runs the suite on every push to `main` and on pull requests, and **fails the build below 90%
overall or 80% in any category**.

### Model comparison

Same 21 scenarios, prompt `v1`, effort `medium`, 2026-10-25. Prices checked against Anthropic's
pricing page the same day. 16 scenarios call the model; the 5 canned ones cost nothing.

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

¹ Warm prompt cache (no cache writes). From a cold cache the Sonnet run costs about $0.17.

**What this does and doesn't show:** the suite doesn't separate the three models. Its scenarios are
deliberately clear-cut, so it works as a regression gate, not a benchmark. On this evidence Haiku
5.5 makes the same routing decisions at about a tenth of Sonnet's cost. Sonnet stays the default
because the suite doesn't yet measure what would justify paying more: email quality, the wording of
risk reasons, and confidence calibration on ambiguous deals. Switch with `MODEL=...`.

## Cost per run

| | Cost |
|---|---|
| One deal assessed (Sonnet 5.5, cached prefix) | about $0.008 |
| Full run of the 29-deal demo org | about $0.23 |
| A deal that already has an open agent Task | $0 (skipped before the model call) |
| Eval gate in CI | about $0.15 per push to `main` |
| Google Cloud | near zero: Cloud Run scales to zero, and BigQuery and Artifact Registry stay in their free tiers. A budget alert emails at 50/90/100% of 5 per month. |

## Run it

**Prerequisites:** Python 3.12 and [uv](https://docs.astral.sh/uv/), a Salesforce Developer Edition
org set up as in [docs/salesforce-setup.md](docs/salesforce-setup.md), and an Anthropic API key.

```bash
uv sync
cp .env.example .env        # fill in SF_DOMAIN, SF_CLIENT_ID, SF_CLIENT_SECRET, ANTHROPIC_API_KEY
uv run python scripts/seed_salesforce.py   # 31 synthetic deals, run as your admin via the sf CLI
uv run deal-desk sf-check                  # auth + every read tool as the integration user
uv run deal-desk run                       # dry run: routes and audits, writes nothing
uv run deal-desk run --write               # creates Tasks for auto_write decisions
uv run python evals/run_evals.py           # eval suite; add --model claude-haiku-5-5 to compare
uv run pytest                              # 59 unit tests, no network
```

Locally the audit log and review queue are JSONL files in `audit_local/`. With `GCP_PROJECT` and
`BQ_DATASET` set they go to BigQuery.

**The service** (`uv run uvicorn deal_desk.api:app`):

| Endpoint | |
|---|---|
| `GET /health` | open; model, prompt version, dry-run flag |
| `POST /runs?limit=50` | run a review over open deals; returns the run summary |
| `GET /reviews` | pending items: latest decision per deal that needs a human |
| `POST /reviews/{run_id}/{opportunity_id}/approve` | writes the drafted Task, or your edited `subject` and `description` |
| `POST /reviews/{run_id}/{opportunity_id}/reject` | records the rejection with an optional note |

**Deploy to your own GCP project:** run [`scripts/gcp_setup.sh`](scripts/gcp_setup.sh) once in Cloud
Shell. It enables the APIs, creates the budget alert, Artifact Registry with a cleanup policy, the
runtime and deploy service accounts, the secrets, the BigQuery dataset, and a Workload Identity pool
restricted to this repository. Add the GitHub variables it prints, and every push to `main` then
tests, evaluates, builds and deploys.

## Salesforce permission lessons

Setting up a least-privilege integration user took three corrections that the docs don't make
obvious. All are recorded in [docs/salesforce-setup.md](docs/salesforce-setup.md).

1. **Access Activities is required.** With only Edit Tasks, Task and Event don't exist for the user
   at all (`INVALID_TYPE: sObject type 'Task' is not supported`), not even in describe.
2. **Task and Event field-level security lives on the profile.** Tasks and Events don't appear in
   permission-set Object Settings. Related To, Due Date and Comments have to be made visible
   (and editable on Task) per field in Object Manager, on the integration user's profile. Without
   that, queries fail with `No such column 'ActivityDate'` and a Task can't be linked to an opportunity.
3. **Edit Tasks also grants delete on the user's own Tasks.** A probe confirmed it, and no separate
   permission removes it. So "the agent never deletes" is enforced in code (no delete method, with a
   test) rather than by Salesforce. In production, a before-delete Apex trigger on Task would close it.

Two smaller notes: Salesforce now restricts creating new Connected Apps, so this integration uses an
External Client App; and the current Salesforce CLI redacts the token in `sf org display` (use
`sf org auth show-access-token`).

## Design decisions

- **OAuth 2.0 Client Credentials on an External Client App.** It's Salesforce's recommended flow for
  server-to-server work. Every call runs as one locked-down integration user, with no certificate
  to manage. JWT bearer would be the choice if the agent had to act as different users.
- **BigQuery over Firestore.** The audit log is something RevOps queries with SQL (cost per week,
  approval rate, which rules fire most), and the role is built around BigQuery. The review queue is
  stored as append-only events in the same dataset rather than as mutable rows, so it needs no second
  database. Writes use plain streaming inserts: simple and reliable at this volume, at about $0.01 per 200 MB.
- **A refusal goes to human review.** The SDK offers a server-side model fallback, but this design
  already has a better fallback for a business decision: a person.
- **Sequential runs on one instance.** About 2 minutes for 29 deals fits inside one request, and an
  in-process lock prevents overlapping runs.

## What I'd do next in production

- **Real reviewer identity with IAP.** Put Identity-Aware Proxy in front of Cloud Run, so approvals
  carry the reviewer's Google identity instead of a configured name, and drop the shared bearer token.
- **Orchestrate the review step as a Camunda BPMN process.** Today the review queue is a table and
  two endpoints. As a BPMN process, each `human_review` decision becomes a user task, with assignment
  by deal owner or region, SLA timers, and escalation to a manager when a high-value item sits unreviewed.
  The process model also documents the approval policy, and the audit trail comes with it.
- **Borderline evals that separate models.** Add graded ambiguous scenarios (mixed signals, partial
  data) and an LLM-judge rubric for email quality and calibration. Then the model choice rests on
  evidence rather than a default.
- **More data sources.** Gainsight health scores and HubSpot engagement data as extra read tools, so
  "at risk" can include product usage and marketing signals, not just CRM activity.
- **An MCP server** exposing the same tools to Claude Desktop and Claude Code, and a TypeScript
  review UI (milestones 5 and 6 of this project).
- **Operations:** a Cloud Scheduler trigger for a daily run, alerting on `audit write failed` and on
  runs that stop early, and a before-delete Apex trigger on Task (see the Salesforce lessons above).

## Repository layout

```text
src/deal_desk/
  guardrails.py   every check and the routing rules
  agent.py        per-deal tool loop, structured output, one retry
  runner.py       a run: search, idempotency, assess, route, write, audit
  review.py       human review queue: pending, approve, reject
  salesforce.py   client-credentials auth, read tools, create_task, retries, API budget
  schemas.py      the model's output contract (Pydantic)
  prompts/v1.md   system prompt, versioned and recorded on every audit row
  api.py          FastAPI service for Cloud Run
  audit.py        JSONL locally, BigQuery on Cloud Run
evals/            scenarios, runner, per-model results
scripts/          Salesforce seed data, one-time GCP setup
docs/             Salesforce setup, step by step
```
