"""Eval suite: labeled fixture scenarios through the real agent loop and guardrails.

No Salesforce needed: a fixture client serves the read tools. Scenarios with `canned_output` or
`canned_raw` replay a fixed (bad) model response instead of calling Claude, so guardrails such as
the grounding check are tested deterministically.

    uv run python evals/run_evals.py                          # model from settings (MODEL env)
    uv run python evals/run_evals.py --model claude-haiku-5-5 --min-pass 0.9 --min-category 0.8

Exits 1 if the overall pass rate or any category's pass rate is below its threshold.
"""

import argparse
import copy
import json
import statistics
import sys
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


def check(expect: dict[str, Any], status: str | None, action: str, reasons: list[str]) -> list[str]:
    misses = []
    if "status" in expect and status != expect["status"]:
        misses.append(f"status {status} != {expect['status']}")
    if action not in expect["decision_in"]:
        misses.append(f"decision {action} not in {expect['decision_in']}")
    wanted = expect.get("reason_contains")
    if wanted and not any(w in r for w in wanted for r in reasons):
        misses.append(f"no reason containing any of {wanted}")
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
    status = result.assessment.status if result.assessment else None
    misses = check(sc["expect"], status, decision.action, decision.reasons)
    return {
        "id": sc["id"], "category": sc["category"], "passed": not misses, "misses": misses,
        "live_model": result.model != "canned", "status": status, "decision": decision.action,
        "reasons": decision.reasons,
        "confidence": result.assessment.confidence if result.assessment else None,
        "error": result.error, "latency_ms": result.latency_ms, "tokens": dict(result.usage),
        "cost_usd": cost_usd(result.model, result.usage) if result.model != "canned" else 0.0,
    }


def p95(values: list[int]) -> int | None:
    v = sorted(values)
    return v[min(len(v) - 1, round(0.95 * (len(v) - 1)))] if v else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--min-pass", type=float, default=0.9)
    parser.add_argument("--min-category", type=float, default=0.8)
    parser.add_argument("--only", help="comma-separated scenario ids")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    settings = get_settings()
    overrides = {k: v for k, v in {"model": args.model, "effort": args.effort}.items() if v}
    settings = settings.model_copy(update=overrides)
    llm = make_llm(settings)
    data = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))
    scenarios = list(enumerate(data["scenarios"], 1))
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [(n, s) for n, s in scenarios if s["id"] in wanted]

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda ns: run_one(ns[1], ns[0], data, llm, settings), scenarios))

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    categories = {c: {"passed": sum(r["passed"] for r in rs), "total": len(rs),
                      "pass_rate": sum(r["passed"] for r in rs) / len(rs)} for c, rs in by_cat.items()}
    live = [r for r in results if r["live_model"]]
    summary = {
        "model": settings.model, "effort": settings.effort, "prompt_version": settings.prompt_version,
        "scenarios": len(results), "passed": sum(r["passed"] for r in results),
        "pass_rate": sum(r["passed"] for r in results) / len(results),
        "categories": categories,
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in results), 4),
        "latency_p50_ms": round(statistics.median([r["latency_ms"] for r in live])) if live else None,
        "latency_p95_ms": p95([r["latency_ms"] for r in live]),
    }

    print(f"\nModel {settings.model} (effort {settings.effort}, prompt {settings.prompt_version})\n")
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  {mark}  {r['id']:<34} {r['decision']:<13} {r['status'] or '-':<8} "
              f"{'; '.join(r['misses'])}")
    print(f"\n{'category':<28} pass")
    for c, s in categories.items():
        print(f"  {c:<26} {s['passed']}/{s['total']}  {s['pass_rate']:.0%}")
    print(f"\n  overall {summary['passed']}/{summary['scenarios']} = {summary['pass_rate']:.0%} | "
          f"cost ${summary['cost_usd']} | p50 {summary['latency_p50_ms']}ms "
          f"p95 {summary['latency_p95_ms']}ms")

    if not args.only:  # partial runs must not overwrite the published results
        out = HERE / "results" / f"{settings.model}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps({"summary": summary, "results": results}, indent=2, default=str),
                       encoding="utf-8")

    failing = [c for c, s in categories.items() if s["pass_rate"] < args.min_category]
    if summary["pass_rate"] < args.min_pass or failing:
        print(f"\nEVAL GATE FAILED: overall {summary['pass_rate']:.0%} (min {args.min_pass:.0%}), "
              f"categories below {args.min_category:.0%}: {failing or 'none'}")
        sys.exit(1)
    print(f"\nEval gate passed (overall >= {args.min_pass:.0%}, every category >= {args.min_category:.0%})")


if __name__ == "__main__":
    main()
