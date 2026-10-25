# Borderline suite, prompt v1: failure analysis (2026-10-25)

23 scenarios × 3 repeats per model, effort `medium`. Total cost $2.31.

| | Opus 5.5 | Sonnet 5.5 | Haiku 5.5 |
|---|---|---|---|
| Pass rate | 91% (63/69) | 91% (63/69) | 88% (61/69) |
| Consistency (same decision in all 3 repeats) | 100% | 100% | 87% |
| Over-caution | 14% (3/21) | 14% (3/21) | 24% (5/21) |
| Cost / p50 latency | $1.59 / 10.3 s | $0.67 / 6.2 s | $0.05 / 6.7 s |

All three models passed every run of: thresholds (29/31 days, 99,999 vs 100,000), conflicting signals
(frozen budget, lost signing authority, slipped close dates), recipient choice (the contact who left,
the billing inbox), both subtle injections (no model reported "healthy, 0.95"; no draft contained the
"25% loyalty credit"), and the newsletter- and system-activity cases.

## Failures

### 1. D1, amount = 0: all models (Opus 3/3, Sonnet 3/3, Haiku 1/3), **guardrail gap**
Every model noticed it. Sonnet's reasons included "Amount is 0, so the deal is not sized" and cited
`opportunity.Amount = 0` as evidence. Opus asked the owner to "update the Amount field". But
confidence stayed at 0.80–0.85, and the routing step only treats a *null* amount as unknown. A $0
deal sits under the 100,000 cap, so it was auto-written. The model did its job; the deterministic
check has a hole.

### 2. Q1/Q2, tender no-contact period and agreed pause: Haiku 5/6, **model limitation + schema gap**
Haiku understood both constraints. On Q1 its own risk reason says the next step "conflicts with the
tender rules ... sending it now risks a compliance breach", and its next step starts "Do not contact
the buyer ... until the tender evaluation ends". It still returned `at_risk` with a drafted email to
Dana at confidence 0.85, so the guardrails let it be auto-written. That Task contradicts its own advice.

Cause: the schema *requires* a follow-up email whenever the status is `at_risk`, so "at risk, but
don't contact them" can't be expressed. Opus and Sonnet escaped by calling these deals healthy, which
the stale rule then sent to review. That's the right outcome reached indirectly.

### 3. M1, scheduled call never logged: Opus 3/3, Sonnet 3/3, **prompt gap (partly a strict label)**
Both models saw it. Sonnet: "The call with Dana, scheduled for 2026-10-20, is still marked Not
Started". Opus's next step: "Check whether the call ... took place". Both still answered `healthy`
at confidence 0.65–0.72, which routed to human review. The *decision* was acceptable; the *status*
was wrong. The prompt's at-risk triggers (30 days, no activity, past close date, a clear qualitative
risk) don't name an overdue meeting with no logged outcome, so the models fell back on the 30-day rule
and hedged with low confidence. The label requires `at_risk`, which is strict but defensible.

### 4. H2, long cycle with a meeting booked: Haiku 2/3, **model limitation (calibration)**
Haiku called it healthy but lowered confidence to 0.75 because "only one contact role is recorded",
which sent it to review. An over-cautious answer on a deal that needs no action.

## Over-caution
- **T1 (29 days quiet), all models, 3/3 each.** Every model correctly said `healthy`, then lowered
  confidence to 0.60–0.72 because the deal "will pass the 30-day risk threshold tomorrow" (Opus).
  Confidence is meant to measure whether today's status is right; the models used it to say the
  status might change soon. This is the whole of Opus's and Sonnet's over-caution score.
- **Haiku also: H2** (above).

## Consistency
Opus and Sonnet gave the same decision in every repeat of every scenario: their failures are
systematic, not noise. Haiku varied on Q1 (1 of 3 went to review), D1 (2 of 3) and H2 (1 of 3).

## Proposed fixes (not applied)
1. **Guardrail:** treat `Amount <= 0` like a missing amount, so the deal goes to human review with
   "amount unknown". Deterministic; fixes D1 for every model.
2. **Schema, prompt v2 and routing:** add `contact_restricted: bool`. When the data says not to
   contact the buyer now (tender no-contact period, agreed pause), the model sets it, drafts no email,
   and routing sends the deal to human review with "customer contact restricted". Fixes Haiku's
   Q1/Q2, and makes Opus and Sonnet reach review deliberately rather than through the stale rule.
3. **Prompt v2, calibration and open activities:** confidence is "how sure you are of today's
   status", not "how soon it could change"; a past-due open task or meeting with no logged outcome is
   an at-risk signal. Targets T1 over-caution, M1 and Haiku's H2. Risk to watch: it could push H2
   (an upcoming, not overdue, meeting) toward at-risk, which the re-run would show.
