import pytest
from fastapi.testclient import TestClient

from deal_desk import api
from deal_desk.audit import JsonlAuditStore
from deal_desk.config import Settings, get_settings
from tests.test_agent import FakeSF
from tests.test_guardrails import OPP_ID
from tests.test_review import queued

TOKEN = "test-token-123"


@pytest.fixture
def client(tmp_path):
    store, sf = JsonlAuditStore(tmp_path), FakeSF()
    settings = Settings(_env_file=None, review_api_token=TOKEN, dry_run=False, reviewer_name="Pat")
    api.app.dependency_overrides = {get_settings: lambda: settings, api.get_store: lambda: store,
                                    api.get_sf: lambda: sf}
    yield TestClient(api.app), store, sf
    api.app.dependency_overrides = {}


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_health_is_open(client):
    c, _, _ = client
    assert c.get("/health").json()["ok"] is True


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_everything_else_needs_the_token(client, headers):
    c, _, _ = client
    for method, path in [("get", "/reviews"), ("post", "/runs"),
                         ("post", f"/reviews/r1/{OPP_ID}/approve"), ("post", f"/reviews/r1/{OPP_ID}/reject")]:
        assert c.request(method, path, headers=headers, json={}).status_code == 401, path


def test_fails_closed_without_a_configured_token(client):
    c, _, _ = client
    api.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    assert c.get("/reviews", headers=AUTH).status_code == 503


def test_review_queue_round_trip(client):
    c, store, sf = client
    queued(store)
    items = c.get("/reviews", headers=AUTH).json()
    assert [i["opportunity_id"] for i in items] == [OPP_ID]
    r = c.post(f"/reviews/r1/{OPP_ID}/approve", headers=AUTH, json={"note": "looks right"})
    assert r.status_code == 200 and r.json()["reviewer"] == "Pat" and len(sf.tasks) == 1
    assert c.get("/reviews", headers=AUTH).json() == []
    assert c.post(f"/reviews/r1/{OPP_ID}/reject", headers=AUTH, json={}).status_code == 404
