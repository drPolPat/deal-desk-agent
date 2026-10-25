import argparse
import json
from datetime import date, timedelta

from deal_desk.agent import make_llm
from deal_desk.audit import make_store
from deal_desk.config import get_settings
from deal_desk.logs import setup_logging
from deal_desk.runner import run
from deal_desk.salesforce import SalesforceClient


def sf_check(args: argparse.Namespace) -> None:
    """Authenticate as the integration user and exercise every read tool once."""
    client = SalesforceClient.from_settings(get_settings())
    opps = client.search_opportunities(limit=args.limit)
    print(f"Authenticated. {len(opps)} open opportunities (oldest activity first):")
    for o in opps:
        print(f"  {o['Id']}  {o['StageName']:<22} {o.get('Amount') or '-':>10}  "
              f"last activity {o.get('LastActivityDate') or 'never':<10}  {o['Name']}")
    if opps:
        first = opps[0]["Id"]
        details = client.get_opportunity_details(first)
        activities = client.get_activity_history(first)
        print(f"\nDetails for {first}:\n{json.dumps(details, indent=2, default=str)}")
        print(f"\n{len(activities)} activities for {first}")
    if args.write_test_task:
        task_id = client.create_task(
            args.write_test_task, "Connectivity test (safe to delete)",
            "Created by `deal-desk sf-check --write-test-task`.", date.today() + timedelta(days=7),
        )
        print(f"\nCreated test Task {task_id} on {args.write_test_task}")
    print(f"\nAPI usage: {client.api_usage()}")


def run_agent(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.write:
        settings = settings.model_copy(update={"dry_run": False})
    llm = make_llm(settings)
    sf = SalesforceClient.from_settings(settings)
    mode = "DRY RUN (no Salesforce writes)" if settings.dry_run else "LIVE (auto_write creates Tasks)"
    print(f"{mode} | model {settings.model} | prompt {settings.prompt_version} | "
          f"auto-write if confidence >= {settings.confidence_threshold} "
          f"and amount < {settings.auto_write_max_amount:,.0f}\n")
    summary = run(settings, sf, llm, make_store(settings), limit=args.limit)
    print("\n" + json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="deal-desk")
    sub = parser.add_subparsers(required=True)
    check = sub.add_parser("sf-check", help="verify Salesforce auth and read tools")
    check.add_argument("--limit", type=int, default=50)
    check.add_argument("--write-test-task", metavar="OPPORTUNITY_ID",
                       help="also create one Task, to confirm the integration user can write Tasks")
    check.set_defaults(func=sf_check)
    agent = sub.add_parser("run", help="review open opportunities (dry run unless --write)")
    agent.add_argument("--limit", type=int, default=50)
    agent.add_argument("--write", action="store_true",
                       help="actually create Tasks for auto_write decisions (overrides DRY_RUN)")
    agent.set_defaults(func=run_agent)
    args = parser.parse_args()
    setup_logging()
    args.func(args)


if __name__ == "__main__":
    main()
