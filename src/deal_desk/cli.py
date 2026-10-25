import argparse
import json
from datetime import date, timedelta

from deal_desk.config import get_settings
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="deal-desk")
    sub = parser.add_subparsers(required=True)
    check = sub.add_parser("sf-check", help="verify Salesforce auth and read tools")
    check.add_argument("--limit", type=int, default=50)
    check.add_argument("--write-test-task", metavar="OPPORTUNITY_ID",
                       help="also create one Task, to confirm the integration user can write Tasks")
    check.set_defaults(func=sf_check)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
