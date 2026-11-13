"""Runtime configuration.

Secrets never live in the database (`source.config` has a `config_no_secrets` check).
They are resolved by `source.id` from the environment: `JOBINDEX_SECRET_<id>` or
`$JOBINDEX_SECRET_DIR/<id>` (ADR 0005). This module holds only process settings.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENT = (
    "JobIndexBot/0.1 (+https://example.invalid/jobindex; contact=ops@example.invalid)"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOBINDEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://jobindex:jobindex@localhost:5432/jobindex"

    # Identifiable UA on every outbound request. A blank UA is a config error, not a default.
    user_agent: str = DEFAULT_USER_AGENT

    http_timeout_s: float = 20.0
    max_concurrency_per_host: int = 1
    request_jitter_ms: int = 250

    log_level: str = "INFO"

    def require_user_agent(self) -> str:
        if not self.user_agent.strip():
            raise ValueError("JOBINDEX_USER_AGENT must be a non-empty identifiable UA")
        return self.user_agent


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
