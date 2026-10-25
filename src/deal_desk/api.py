"""HTTP service for Cloud Run: trigger runs and work the human review queue.

Every endpoint except /healthz needs `Authorization: Bearer <REVIEW_API_TOKEN>`. With no token
configured, those endpoints refuse every request (fail closed).
"""

import secrets
import threading
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from deal_desk.agent import make_llm
from deal_desk.audit import AuditStore, make_store
from deal_desk.config import Settings, get_settings
from deal_desk.logs import setup_logging
from deal_desk.review import ReviewError, approve, pending, reject
from deal_desk.runner import run
from deal_desk.salesforce import SalesforceClient

setup_logging()
app = FastAPI(title="Deal Desk Agent", version="0.1.0")
_run_lock = threading.Lock()  # one run at a time (the service runs with max-instances=1)


@lru_cache
def get_store() -> AuditStore:
    return make_store(get_settings())


@lru_cache
def get_sf() -> SalesforceClient:
    return SalesforceClient.from_settings(get_settings())


def require_token(authorization: Annotated[str, Header()] = "",
                  settings: Settings = Depends(get_settings)) -> None:
    expected = settings.review_api_token.get_secret_value()
    if not expected:
        raise HTTPException(503, "REVIEW_API_TOKEN is not configured")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {expected}".encode()):
        raise HTTPException(401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})


Auth = Depends(require_token)


class ApproveBody(BaseModel):
    subject: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=30000)
    note: str | None = Field(None, max_length=2000)


class RejectBody(BaseModel):
    note: str | None = Field(None, max_length=2000)


@app.get("/healthz")
def healthz(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return {"ok": True, "model": settings.model, "prompt_version": settings.prompt_version,
            "dry_run": settings.dry_run}


@app.post("/runs", dependencies=[Auth])
def start_run(limit: int = Query(50, ge=1, le=200), settings: Settings = Depends(get_settings),
              store: AuditStore = Depends(get_store), sf: SalesforceClient = Depends(get_sf)) -> dict:
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "a run is already in progress")
    try:
        return run(settings, sf, make_llm(settings), store, limit=limit)
    finally:
        _run_lock.release()


@app.get("/reviews", dependencies=[Auth])
def list_reviews(store: AuditStore = Depends(get_store)) -> list[dict[str, Any]]:
    keys = ("run_id", "opportunity_id", "opportunity_name", "amount", "ts", "status", "confidence",
            "reasons", "rule_flags", "assessment", "error", "model", "prompt_version")
    return [{k: item.get(k) for k in keys} for item in pending(store)]


@app.post("/reviews/{run_id}/{opportunity_id}/approve", dependencies=[Auth])
def approve_item(run_id: str, opportunity_id: str, body: ApproveBody,
                 settings: Settings = Depends(get_settings), store: AuditStore = Depends(get_store),
                 sf: SalesforceClient = Depends(get_sf)) -> dict[str, Any]:
    try:
        return approve(store, sf, settings, run_id, opportunity_id, subject=body.subject,
                       description=body.description, note=body.note)
    except ReviewError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@app.post("/reviews/{run_id}/{opportunity_id}/reject", dependencies=[Auth])
def reject_item(run_id: str, opportunity_id: str, body: RejectBody,
                settings: Settings = Depends(get_settings),
                store: AuditStore = Depends(get_store)) -> dict[str, Any]:
    try:
        return reject(store, settings, run_id, opportunity_id, note=body.note)
    except ReviewError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
