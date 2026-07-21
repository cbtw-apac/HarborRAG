"""Runtime process settings from HARBORRAG_* env vars (ST8).

Imported lazily by CompositionRoot.production() only — pydantic-settings is
part of the [production] extra, and the bare CLI install must keep working
without it.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    """Environment-driven settings for runtime composition."""

    model_config = SettingsConfigDict(env_prefix="HARBORRAG_", extra="ignore")

    env: Literal["dev", "prod"] = "dev"
    control_db_url: str = "sqlite+aiosqlite:///./harborrag_control.db"
    qdrant_url: str | None = None
    redis_url: str | None = None
    secrets_backend: Literal["env", "file"] = "env"


DEFAULT_CONTROL_DB_URL = RuntimeSettings.model_fields["control_db_url"].default
