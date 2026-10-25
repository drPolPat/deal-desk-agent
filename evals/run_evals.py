"""Eval suites: labeled fixture scenarios through the real agent loop and guardrails.

No Salesforce needed: a fixture client serves the read tools. Scenarios with `canned_output` or
`canned_raw` replay a fixed (bad) model response instead of calling Claude, so guardrails such as
the grounding check are tested deterministically.

Two suites:
  regression  (evals/scenarios.json)  clear-cut cases; the CI gate. Exits 1 below the thresholds.
  borderline  (evals/borderline.json) judgment calls; manual only, never fails a build. Reports
              consistency across repeats and an over-caution rate next to the pass rate.

    uv run python evals/run_evals.py                                   # regression, MODEL from settings
    uv run python evals/run_evals.py --model claude-haiku-5-5
    uv run python evals/run_evals.py --suite borderline --repeats 3 \\
        --models claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5 --budget-usd 3.50

Results: evals/results/<suite>/<prompt_version>/<model>.json, plus summary.md for the suite.
"""

import argparse
import copy
import json
import statistics
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

from deal_desk.agent import assess, make_llm
from deal_desk.config import get_settings
from deal_desk.costs import cost_usd
from deal_desk.runner import decide

HERE = Path(__file__).parent
SUITES = {"regression": "scenarios.json", "borderline": "borderline.json"}

CANNED_BASE = {
    "status": "at_risk", "risk_reasons": ["No activity in over 30 days"], "confidence": 0.9,
    "next_step": "Call Dana Whitfield this week to confirm the timeline",
    "follow_up_email": {"to_contact_name": "Dana Whitfield", "subject": "Checking in on next steps",
                        "body": "Hi Dana, could we find 20 minutes this week to agree on next steps?"},
    "task_due_in_days": 3,
}


class FixtureSalesforce:
    def __init__(self, opportunity: dict[str, Any], activities: list[dict[str, Any]]):
        self.opportunity, self.activities = opportunity, activities

    def get_opportunity_details(self, opportunity_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.opportunity)

    def get_activity_history(self, opportunity_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.activities)


class CannedLLM:
    """Replays fixed final answers; stands in for anthropic.Anthropic in assess()."""

    def __init__(self, texts: list[str]):
        self.texts = list(texts)
        self.messages = self

    def create(self, **_: Any) -> NS:
        usage = NS(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return NS(stop_reason="end_turn", model="canned", usage=usage,
                  content=[NS(type="text", text=self.texts.pop(0))])


def build(sc: dict[str, Any], n: int, defaults: dict[str, Any]) -> tuple[dict, list]:
    opp_id = f"006EVAL{n:08d}"
    opp = {**copy.deepcopy(defaults["opportunity"]), **sc["opportunity"], "Id": opp_id}
    activities = [{"Kind": "Task", "Id": f"00TEVAL{n:04d}{i:04d}", "Type": None, "Description": None,
                   "CreatedDate": f"{a['ActivityDate']}T09:00:00.000+0000", **a}
                  for i, a in enumerate(sc["activities"])]
    return opp, activities


def check(expect: dict[str, Any], status: str | None, action: str, reasons: list[str],
          email: Any = None) -> list[str]:
    misses = []
    status_in = expect.get("status_in") or ([expect["status"]] if "status" in expect else None)
    if status_in and status not in status_in:
        misses.append(f"status {status} not in {status_in}")
    if action not in expect["decision_in"]:
        misses.append(f"decision {action} not in {expect['decision_in']}")
    wanted = expect.get("reason_contains")
    if wanted and not any(w in r for w in wanted for r in reasons):
        misses.append(f"no reason containing any of {wanted}")
    banned = expect.get("reason_not_contains")
    if banned and any(b in r for b in banned for r in reasons):
        misses.append(f"routed for a reason it shouldn't be: {banned}")
    if email:  # recipient and content checks apply whenever the model drafted an email
        if "recipient_in" in expect and email.to_contact_name not in expect["recipient_in"]:
            misses.append(f"email to {email.to_contact_name!r}, expected one of {expect['recipient_in']}")
        if email.to_contact_name in expect.get("recipient_not", []):
            misses.append(f"email to {email.to_contact_name!r}, who is the wrong recipient")
        text = f"{email.subject} {email.body}".lower()
        hits = [w for w in expect.get("email_must_not_contain", []) if w.lower() in text]
        if hits:
            misses.append(f"email contains {hits}")
    return misses


def run_one(sc: dict[str, Any], n: int, data: dict[str, Any], llm: Any, settings: Any) -> dict[str, Any]:
    today = date.fromisoformat(data["today"])
    opp, activities = build(sc, n, data["defaults"])
    if "canned_output" in sc:
        llm = CannedLLM([json.dumps({**CANNED_BASE, "opportunity_id": opp["Id"], **sc["canned_output"]})])
    elif "canned_raw" in sc:
        llm = CannedLLM(sc["canned_raw"])
    result = assess(llm, FixtureSalesforce(opp, activities), opp["Id"], settings, today)
    decision = decide(result, settings, today)
    a = result.assessment
    misses = check(sc["expect"], a.status if a else None, decision.action, decision.reasons,
                   a.follow_up_email if a else None)
    return {
        "id": sc["id"], "category": sc["category"], "passed": not misses, "misses": misses,
        "live_model": result.model != "canned", "status": a.status if a else None,
        "decision": decision.action, "reasons": decision.reasons, "rule_flags": decision.flags,
        "confidence": a.confidence if a else None, "assessment": a.model_dump() if a else None,
        "error": result.error, "latency_ms": result.latency_ms, "tokens": dict(result.usage),
        "cost_usd": cost_usd(result.model, result.usage) if result.model != "canned" else 0.0,
    }


def p95(values: list[int]) -> int | None:
    v = sorted(values)
    return v[min(len(v) - 1, round(0.95 * (len(v) - 1)))] if v else None


def summarize(results: list[dict[str, Any]], scenarios: dict[str, dict[str, Any]],
              settings: Any, repeats: int) -> dict[str, Any]:
    runs = [r for r in results if not r.get("skipped")]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    by_sc: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        by_cat[r["category"]].append(r)
        by_sc[r["id"]].append(r)
    categories = {c: {"passed": sum(r["passed"] for r in rs), "total": len(rs),
                      "pass_rate": sum(r["passed"] for r in rs) / len(rs)} for c, rs in by_cat.items()}
    # Over-caution: escalating (human_review) or flagging risk where no_action or auto_write was the
    # single best answer. Keeps a model from scoring well just by escalating everything.
    best = {i: sc["expect"].get("best") for i, sc in scenarios.items()}
    eligible = [r for r in runs if best[r["id"]] in ("no_action", "auto_write")]
    over = [r for r in eligible if r["decision"] == "human_review"
            or (best[r["id"]] == "no_action" and r["status"] == "at_risk")]
    live = [r for r in runs if r["live_model"]]
    n_sc = len(by_sc) or 1
    return {
        "model": settings.model, "effort": settings.effort, "prompt_version": settings.prompt_version,
        "repeats": repeats, "scenarios": len(by_sc), "runs": len(runs), "skipped": len(results) - len(runs),
        "passed": sum(r["passed"] for r in runs),
        "pass_rate": sum(r["passed"] for r in runs) / len(runs) if runs else 0.0,
        "categories": categories,
        "consistency": sum(len({r["decision"] for r in rs}) == 1 for rs in by_sc.values()) / n_sc,
        "stable_pass": sum(all(r["passed"] for r in rs) for rs in by_sc.values()) / n_sc,
        "over_caution": len(over) / len(eligible) if eligible else None,
        "over_caution_runs": f"{len(over)}/{len(eligible)}",
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in runs), 4),
        "latency_p50_ms": round(statistics.median([r["latency_ms"] for r in live])) if live else None,
        "latency_p95_ms": p95([r["latency_ms"] for r in live]),
    }


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def write_summary_md(out_dir: Path) -> None:
    """One markdown table covering every model with results in this suite/prompt folder."""
    sums = [json.loads(f.read_text(encoding="utf-8"))["summary"] for f in sorted(out_dir.glob("*.json"))]
    if not sums:
        return
    cats = sorted({c for s in sums for c in s["categories"]})
    row = lambda label, cells: f"| {label} | " + " | ".join(cells) + " |"  # noqa: E731
    lines = [row("", [s["model"] for s in sums]), "|---" * (len(sums) + 1) + "|",
             row("**Pass rate (all runs)**",
                 [f"**{_pct(s['pass_rate'])}** ({s['passed']}/{s.get('runs', s['scenarios'])})"
                  for s in sums])]
    for c in cats:
        lines.append(row(c.replace("_", " "), [
            f"{s['categories'][c]['passed']}/{s['categories'][c]['total']}" if c in s["categories"] else "-"
            for s in sums]))
    lines += [
        # .get(): results written before these metrics existed (regression v1) don't have them
        row("Consistency (same decision every repeat)", [_pct(s.get("consistency")) for s in sums]),
        row("Stable pass (passed every repeat)", [_pct(s.get("stable_pass")) for s in sums]),
        row("Over-caution (lower is better)",
            [f"{_pct(s.get('over_caution'))} ({s.get('over_caution_runs', '-')})" for s in sums]),
        row("Cost for the suite", [f"${s['cost_usd']:.2f}" for s in sums]),
        row("Latency p50 / p95", [f"{s['latency_p50_ms'] / 1000:.1f} s / {s['latency_p95_ms'] / 1000:.1f} s"
                                  if s["latency_p50_ms"] else "-" for s in sums]),
    ]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_model(model: str, args: argparse.Namespace, data: dict[str, Any],
              scenarios: list[tuple[int, dict]], budget: dict[str, float]) -> dict[str, Any]:
    overrides = {k: v for k, v in {"model": model, "effort": args.effort}.items() if v}
    settings = get_settings().model_copy(update=overrides)
    llm = make_llm(settings)
    lock = threading.Lock()
    jobs = [(n, sc, rep) for n, sc in scenarios for rep in range(args.repeats)]

    def job(item: tuple[int, dict, int]) -> dict[str, Any]:
        n, sc, rep = item
        with lock:  # shortcut: in-flight calls can overshoot the cap by up to `workers` calls
            if budget["spent"] >= budget["cap"]:
                return {"id": sc["id"], "category": sc["category"], "repeat": rep, "skipped": True}
        r = {**run_one(sc, n, data, llm, settings), "repeat": rep}
        with lock:
            budget["spent"] += r["cost_usd"] or 0
        return r

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(job, jobs))
    summary = summarize(results, {sc["id"]: sc for _, sc in scenarios}, settings, args.repeats)

    print(f"\nModel {settings.model} (effort {settings.effort}, prompt {settings.prompt_version}, "
          f"{args.repeats} repeat(s))\n")
    for r in results:
        if r.get("skipped"):
            print(f"  SKIP  {r['id']:<44} (budget cap reached)")
            continue
        print(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['id']:<44} {r['decision']:<16} "
              f"{r['status'] or '-':<8} {'; '.join(r['misses'])}")
    print(f"\n{'category':<28} pass")
    for c, s in summary["categories"].items():
        print(f"  {c:<26} {s['passed']}/{s['total']}  {s['pass_rate']:.0%}")
    print(f"\n  overall {summary['passed']}/{summary['runs']} = {summary['pass_rate']:.0%} | "
          f"consistency {_pct(summary['consistency'])} | stable {_pct(summary['stable_pass'])} | "
          f"over-caution {summary['over_caution_runs']} | cost ${summary['cost_usd']} | "
          f"p50 {summary['latency_p50_ms']}ms p95 {summary['latency_p95_ms']}ms")

    if not args.only:  # partial runs must not overwrite the published results
        out_dir = HERE / "results" / args.suite / settings.prompt_version
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{settings.model}.json").write_text(
            json.dumps({"summary": summary, "results": results}, indent=2, default=str), encoding="utf-8")
        write_summary_md(out_dir)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", choices=sorted(SUITES), default="regression")
    parser.add_argument("--model", help="one model (default: MODEL from settings)")
    parser.add_argument("--models", help="comma-separated models, run one after another")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--effort")
    parser.add_argument("--budget-usd", type=float, default=None,
                        help="stop starting new calls once this much has been spent (across models)")
    parser.add_argument("--min-pass", type=float, default=0.9)
    parser.add_argument("--min-category", type=float, default=0.8)
    parser.add_argument("--only", help="comma-separated scenario ids")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    data = json.loads((HERE / SUITES[args.suite]).read_text(encoding="utf-8"))
    scenarios = list(enumerate(data["scenarios"], 1))
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [(n, s) for n, s in scenarios if s["id"] in wanted]
    models = ([m.strip() for m in args.models.split(",")] if args.models
              else [args.model or get_settings().model])
    budget = {"spent": 0.0, "cap": args.budget_usd if args.budget_usd is not None else float("inf")}
    summaries = [run_model(m, args, data, scenarios, budget) for m in models]
    print(f"\nTotal spent: ${budget['spent']:.2f}")

    if args.suite != "regression":
        return  # the borderline suite measures; it never gates a build
    low = [s["model"] for s in summaries if s["pass_rate"] < args.min_pass]
    failing = [(s["model"], c) for s in summaries for c, cs in s["categories"].items()
               if cs["pass_rate"] < args.min_category]
    if low or failing:
        print(f"\nEVAL GATE FAILED: below {args.min_pass:.0%} overall: {low or 'none'}; "
              f"categories below {args.min_category:.0%}: {failing or 'none'}")
        sys.exit(1)
    print(f"\nEval gate passed (overall >= {args.min_pass:.0%}, every category >= {args.min_category:.0%})")


if __name__ == "__main__":
    main()
