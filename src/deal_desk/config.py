from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime config. Locally from .env; on Cloud Run from env vars backed by Secret Manager."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    sf_domain: str = ""  # My Domain host, e.g. yourname-dev-ed.develop.my.salesforce.com
    sf_client_id: SecretStr = SecretStr("")
    sf_client_secret: SecretStr = SecretStr("")
    sf_api_version: str = "64.0"
    sf_timeout_seconds: float = 15.0
    sf_max_retries: int = 3
    # Stop a run before the org's daily API allowance runs out (other integrations share it).
    sf_api_usage_max_fraction: float = 0.8

    anthropic_api_key: SecretStr = SecretStr("")
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"  # output_config.effort: low | medium | high
    prompt_version: str = "v1"
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 3  # SDK retries 429/5xx/connection errors with backoff
    max_agent_turns: int = 6
    max_output_tokens: int = 8000

    # Routing: a Task is written automatically only if ALL of these hold (see guardrails.route).
    confidence_threshold: float = 0.8
    auto_write_max_amount: float = 100_000
    stale_days: int = 30

    dry_run: bool = True  # route and audit, but never write to Salesforce
    audit_dir: str = "audit_local"
    # Set both to use BigQuery for the audit log and review queue (Cloud Run); else JSONL in audit_dir.
    gcp_project: str = ""
    bq_dataset: str = ""

    # Review API: bearer token (Secret Manager on Cloud Run) and the name recorded as approver.
    review_api_token: SecretStr = SecretStr("")
    reviewer_name: str = "reviewer"


@lru_cache
def get_settings() -> Settings:
    return Settings()
