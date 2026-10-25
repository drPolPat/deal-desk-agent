"""Salesforce access for the agent.

Least privilege is enforced in three layers:
  1. Salesforce: the integration user's permission set grants read on Account/Contact/
     Opportunity and Task create only (see README > Salesforce setup).
  2. This module: SalesforceClient exposes reads plus create_task. There is no update or
     delete method, so no code path in this app can edit an Opportunity.
  3. The agent: the model is only given the read tools. create_task is called by routing
     code after the guardrails pass or a human approves.
"""

import logging
import random
import re
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import requests
from simple_salesforce import Salesforce, format_soql
from simple_salesforce.exceptions import (
    SalesforceGeneralError,
    SalesforceRefusedRequest,
    SalesforceResourceNotFound,
)

from deal_desk.config import Settings

log = logging.getLogger(__name__)

OPPORTUNITY_SUMMARY_FIELDS = [
    "Id", "Name", "StageName", "Amount", "CloseDate", "Probability",
    "LastActivityDate", "Account.Name",
]
OPPORTUNITY_DETAIL_FIELDS = OPPORTUNITY_SUMMARY_FIELDS + [
    "AccountId", "Type", "LeadSource", "NextStep", "Description", "ForecastCategoryName",
    "IsClosed", "CreatedDate", "LastModifiedDate", "Owner.Name", "Account.Industry",
]
OPPORTUNITY_ID_RE = re.compile(r"^006[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$")
TASK_SUBJECT_PREFIX = "[Deal Desk Agent] "


class SalesforceUnavailable(Exception):
    """Salesforce kept failing after retries (network errors or 5xx)."""


class ApiLimitExceeded(Exception):
    """The org's API allowance is exhausted, or this app's share of it is used up."""


class InvalidInput(ValueError):
    """Bad argument to a tool, e.g. a malformed record Id. Never retried."""


class _TimeoutSession(requests.Session):
    """requests has no session-wide timeout; without one a hung socket blocks a run forever."""

    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        kwargs.setdefault("timeout", self.timeout)
        return super().request(*args, **kwargs)


def _error_code(exc: Exception) -> str | None:
    content = getattr(exc, "content", None)
    if isinstance(content, list) and content and isinstance(content[0], dict):
        return content[0].get("errorCode")
    return None


def with_retry[T](
    fn: Callable[[], T],
    max_retries: int,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry transient failures (network, timeouts, 5xx) with exponential backoff and jitter.

    4xx errors are not retried: they will not succeed on a second try. API-limit errors are
    turned into ApiLimitExceeded so a run can stop cleanly instead of half-finishing.
    """
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except SalesforceRefusedRequest as exc:
            if _error_code(exc) == "REQUEST_LIMIT_EXCEEDED":
                raise ApiLimitExceeded("Salesforce org API request limit exceeded") from exc
            raise
        except (requests.ConnectionError, requests.Timeout, SalesforceGeneralError) as exc:
            if isinstance(exc, SalesforceGeneralError) and exc.status < 500:
                raise
            if attempt == max_retries:
                raise SalesforceUnavailable(f"Salesforce call failed after retries: {exc}") from exc
            delay = base_delay * 2**attempt + random.uniform(0, base_delay)  # noqa: S311
            log.warning("salesforce retry", extra={"attempt": attempt + 1, "delay_s": delay})
            sleep(delay)
    raise AssertionError("unreachable")


def _clean(value: Any) -> Any:
    """Drop Salesforce 'attributes' metadata and flatten child-relationship query results."""
    if isinstance(value, dict):
        if "records" in value and "totalSize" in value:
            return [_clean(r) for r in value["records"]]
        return {k: _clean(v) for k, v in value.items() if k != "attributes"}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _check_opportunity_id(opportunity_id: str) -> str:
    if not isinstance(opportunity_id, str) or not OPPORTUNITY_ID_RE.match(opportunity_id):
        raise InvalidInput(f"Not a valid Opportunity Id: {opportunity_id!r}")
    return opportunity_id


class SalesforceClient:
    def __init__(self, sf: Salesforce, settings: Settings):
        self.sf = sf
        self.settings = settings

    @classmethod
    def from_settings(cls, settings: Settings) -> "SalesforceClient":
        if not (settings.sf_domain and settings.sf_client_id.get_secret_value()):
            raise RuntimeError("SF_DOMAIN / SF_CLIENT_ID / SF_CLIENT_SECRET are not set (see .env.example)")
        # simple-salesforce builds https://{domain}.salesforce.com/services/oauth2/token and
        # uses the client credentials flow when given a consumer key/secret and a My Domain.
        domain = settings.sf_domain.removeprefix("https://").removesuffix("/")
        domain = domain.removesuffix(".salesforce.com")
        sf = with_retry(
            lambda: Salesforce(
                consumer_key=settings.sf_client_id.get_secret_value(),
                consumer_secret=settings.sf_client_secret.get_secret_value(),
                domain=domain,
                version=settings.sf_api_version,
                session=_TimeoutSession(settings.sf_timeout_seconds),
            ),
            settings.sf_max_retries,
        )
        return cls(sf, settings)

    # -- plumbing ---------------------------------------------------------------------------

    def api_usage(self) -> dict[str, int] | None:
        usage = self.sf.api_usage.get("api-usage")
        return {"used": usage.used, "total": usage.total} if usage else None

    def _call[T](self, fn: Callable[[], T], retry: bool = True) -> T:
        usage = self.api_usage()
        if usage and usage["used"] >= usage["total"] * self.settings.sf_api_usage_max_fraction:
            raise ApiLimitExceeded(
                f"API usage {usage['used']}/{usage['total']} is above this app's "
                f"{self.settings.sf_api_usage_max_fraction:.0%} budget"
            )
        return with_retry(fn, self.settings.sf_max_retries if retry else 0)

    def _query(self, soql: str) -> list[dict[str, Any]]:
        return _clean(self._call(lambda: self.sf.query_all(soql))["records"])

    # -- read tools -------------------------------------------------------------------------

    def search_opportunities(
        self, *, stage: str | None = None, min_amount: float | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Open opportunities, oldest activity first (never-touched deals first)."""
        where = ["IsClosed = false"]
        if stage:
            where.append(format_soql("StageName = {}", stage))
        if min_amount is not None:
            where.append(format_soql("Amount >= {}", float(min_amount)))
        limit = max(1, min(int(limit), 200))
        return self._query(
            f"SELECT {', '.join(OPPORTUNITY_SUMMARY_FIELDS)} FROM Opportunity "  # noqa: S608
            f"WHERE {' AND '.join(where)} ORDER BY LastActivityDate ASC NULLS FIRST LIMIT {limit}"
        )

    def get_opportunity_details(self, opportunity_id: str) -> dict[str, Any]:
        """One opportunity with its account and contact roles."""
        _check_opportunity_id(opportunity_id)
        rows = self._query(
            format_soql(
                f"SELECT {', '.join(OPPORTUNITY_DETAIL_FIELDS)}, "  # noqa: S608
                "(SELECT Role, IsPrimary, Contact.Name, Contact.Title, Contact.Email "
                "FROM OpportunityContactRoles) FROM Opportunity WHERE Id = {}",
                opportunity_id,
            )
        )
        if not rows:
            raise SalesforceResourceNotFound("", 404, "Opportunity", opportunity_id.encode())
        opp = rows[0]
        opp["OpportunityContactRoles"] = opp.get("OpportunityContactRoles") or []
        return opp

    def get_activity_history(self, opportunity_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Tasks and events on an opportunity, newest first."""
        _check_opportunity_id(opportunity_id)
        limit = max(1, min(int(limit), 50))
        tasks = self._query(
            format_soql(
                "SELECT Id, Subject, Status, ActivityDate, Type, Description, CreatedDate "
                "FROM Task WHERE WhatId = {} ORDER BY ActivityDate DESC NULLS LAST LIMIT {}",
                opportunity_id,
                limit,
            )
        )
        events = self._query(
            format_soql(
                "SELECT Id, Subject, ActivityDate, Type, Description, CreatedDate "
                "FROM Event WHERE WhatId = {} ORDER BY ActivityDate DESC NULLS LAST LIMIT {}",
                opportunity_id,
                limit,
            )
        )
        activities = [{"Kind": "Task", **t} for t in tasks] + [{"Kind": "Event", **e} for e in events]
        activities.sort(key=lambda a: a.get("ActivityDate") or "", reverse=True)
        return activities[:limit]

    def has_open_agent_task(self, opportunity_id: str) -> bool:
        """True if this app already left an open Task on the opportunity (idempotency check)."""
        _check_opportunity_id(opportunity_id)
        soql = format_soql(
            "SELECT Id FROM Task WHERE WhatId = {} AND IsClosed = false "
            "AND Subject LIKE '{:like}%' LIMIT 1",
            opportunity_id,
            TASK_SUBJECT_PREFIX,
        )
        return bool(self._query(soql))

    # -- the only write ---------------------------------------------------------------------

    def create_task(
        self, opportunity_id: str, subject: str, description: str, due_date: date
    ) -> str:
        """Create an open follow-up Task on the opportunity. Returns the new Task Id.

        Open tasks do not change Opportunity.LastActivityDate (only completed tasks and past
        events do), so the agent's own tasks never make a stale deal look healthy.
        """
        _check_opportunity_id(opportunity_id)
        subject = subject.strip()
        if not subject:
            raise InvalidInput("Task subject is empty")
        full_subject = (TASK_SUBJECT_PREFIX + subject)[:255]
        result = self._call(
            lambda: self.sf.Task.create(
                {
                    "WhatId": opportunity_id,
                    "Subject": full_subject,
                    "Description": description[:32000],
                    "ActivityDate": due_date.isoformat(),
                    "Status": "Not Started",
                    "Priority": "Normal",
                }
            ),
            # Not retried: a timeout after Salesforce committed the insert would duplicate the
            # Task. The caller records the failure and the item stays in the review queue.
            retry=False,
        )
        if not result.get("success"):
            raise SalesforceUnavailable(f"Task create failed: {result.get('errors')}")
        return result["id"]
