from datetime import date
from types import SimpleNamespace

import pytest
import requests
from simple_salesforce.exceptions import (
    SalesforceGeneralError,
    SalesforceMalformedRequest,
    SalesforceRefusedRequest,
)
from simple_salesforce.util import Usage

from deal_desk.config import Settings
from deal_desk.salesforce import (
    ApiLimitExceeded,
    InvalidInput,
    SalesforceClient,
    SalesforceUnavailable,
    _clean,
    with_retry,
)

OPP_ID = "006Hs00001AbCdEIAV"


def flaky(*failures):
    """Callable that raises each given exception once, then returns 'ok'."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= len(failures):
            raise failures[calls["n"] - 1]
        return "ok"

    fn.calls = calls
    return fn


def sf_error(cls, status, code="X"):
    return cls("url", status, "query", [{"errorCode": code, "message": "m"}])


def test_retries_transient_errors_then_succeeds():
    fn = flaky(requests.Timeout(), sf_error(SalesforceGeneralError, 503))
    assert with_retry(fn, max_retries=3, sleep=lambda _: None) == "ok"
    assert fn.calls["n"] == 3


def test_gives_up_after_max_retries():
    fn = flaky(*[requests.ConnectionError()] * 5)
    with pytest.raises(SalesforceUnavailable):
        with_retry(fn, max_retries=2, sleep=lambda _: None)
    assert fn.calls["n"] == 3


def test_does_not_retry_client_errors():
    fn = flaky(sf_error(SalesforceMalformedRequest, 400, "MALFORMED_QUERY"))
    with pytest.raises(SalesforceMalformedRequest):
        with_retry(fn, max_retries=3, sleep=lambda _: None)
    assert fn.calls["n"] == 1


def test_api_limit_becomes_clean_stop():
    fn = flaky(sf_error(SalesforceRefusedRequest, 403, "REQUEST_LIMIT_EXCEEDED"))
    with pytest.raises(ApiLimitExceeded):
        with_retry(fn, max_retries=3, sleep=lambda _: None)
    assert fn.calls["n"] == 1


class FakeSF:
    def __init__(self, records=None, used=10, total=15000):
        self.records = records or []
        self.soql = []
        self.api_usage = {"api-usage": Usage(used=used, total=total)}
        self.Task = SimpleNamespace(create=self._create)
        self.created = []

    def query_all(self, soql):
        self.soql.append(soql)
        return {"records": self.records}

    def _create(self, record):
        self.created.append(record)
        raise requests.Timeout()


def client(fake):
    return SalesforceClient(fake, Settings(sf_max_retries=2, _env_file=None))


def test_search_escapes_model_supplied_stage():
    fake = FakeSF()
    client(fake).search_opportunities(stage="x' OR Name != '", limit=10)
    assert "StageName = 'x\\' OR Name != \\''" in fake.soql[0]
    assert "IsClosed = false" in fake.soql[0]


def test_activity_history_queries_tasks_and_events_with_clamped_limit():
    fake = FakeSF()
    client(fake).get_activity_history(OPP_ID, limit=500)
    assert [q.split(" FROM ")[1].split()[0] for q in fake.soql] == ["Task", "Event"]
    assert all(f"WhatId = '{OPP_ID}'" in q and q.endswith("LIMIT 50") for q in fake.soql)


def test_rejects_malformed_ids_before_calling_salesforce():
    fake = FakeSF()
    for bad in ["001Hs00001AbCdEIAV", "006' OR Id != '", "", "006short"]:
        with pytest.raises(InvalidInput):
            client(fake).get_activity_history(bad)
    assert fake.soql == []


def test_stops_before_exhausting_org_api_budget():
    fake = FakeSF(used=12500, total=15000)  # above the default 80% share
    with pytest.raises(ApiLimitExceeded):
        client(fake).search_opportunities()
    assert fake.soql == []


def test_create_task_is_never_retried():
    fake = FakeSF()
    with pytest.raises(SalesforceUnavailable):
        client(fake).create_task(OPP_ID, "Follow up", "body", date(2026, 11, 1))
    assert len(fake.created) == 1
    assert fake.created[0]["Subject"].startswith("[Deal Desk Agent] ")
    assert fake.created[0]["Status"] == "Not Started"


def test_client_has_no_update_or_delete_methods():
    public = {m for m in dir(SalesforceClient) if not m.startswith("_")}
    assert public == {"from_settings", "api_usage", "search_opportunities",
                      "get_opportunity_details", "get_activity_history", "create_task"}


def test_clean_flattens_child_relationships():
    raw = {"attributes": {"type": "Opportunity"}, "Id": OPP_ID,
           "Account": {"attributes": {}, "Name": "Acme"},
           "OpportunityContactRoles": {"totalSize": 1, "done": True,
                                       "records": [{"attributes": {}, "Role": "Decision Maker"}]}}
    assert _clean(raw) == {"Id": OPP_ID, "Account": {"Name": "Acme"},
                           "OpportunityContactRoles": [{"Role": "Decision Maker"}]}
