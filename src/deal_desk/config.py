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


@lru_cache
def get_settings() -> Settings:
    return Settings()
