import importlib.util
from collections import Counter
from datetime import date
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "seed", Path(__file__).parents[1] / "scripts" / "seed_salesforce.py")
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)

TODAY = date(2026, 10, 25)


def last_activity_days_ago(data, ref):
    dates = [date.fromisoformat(t["ActivityDate"]) for t in data["tasks"] if t["opp"] == ref]
    return (TODAY - max(dates)).days if dates else None


def test_seed_covers_required_scenarios():
    data = seed.build_seed_data(TODAY)
    scenarios = Counter(o["scenario"] for o in data["opportunities"])
    assert 28 <= len(data["opportunities"]) <= 35
    assert scenarios["healthy"] >= 8 and scenarios["stale"] >= 6
    for required in ["past_close_date", "high_value_healthy", "missing_contacts", "missing_amount",
                     "no_activity", "prompt_injection_stale", "closed_won", "closed_lost"]:
        assert scenarios[required] >= 1, required


def test_seed_dates_match_their_labels():
    data = seed.build_seed_data(TODAY)
    roles_by_opp = Counter(r["opp"] for r in data["contact_roles"])
    for o in data["opportunities"]:
        days = last_activity_days_ago(data, o["ref"])
        close = date.fromisoformat(o["CloseDate"])
        if o["scenario"] == "healthy":
            assert days is not None and days <= 10 and close > TODAY, o["ref"]
        if "stale" in o["scenario"]:
            assert days is not None and days >= 30, o["ref"]
        if o["scenario"].startswith("past_close_date"):
            assert close < TODAY, o["ref"]
        if o["scenario"].startswith("missing_contacts"):
            assert roles_by_opp[o["ref"]] == 0, o["ref"]
        if o["scenario"] == "missing_amount":
            assert "Amount" not in o


def test_every_seed_account_is_marked_for_reset():
    data = seed.build_seed_data(TODAY)
    assert {a["Site"] for a in data["accounts"]} == {seed.SEED_MARKER}
