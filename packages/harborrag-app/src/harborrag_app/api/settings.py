"""API process settings, loaded from HARBORRAG_* environment variables (ST2).

Static process config only (ports, auth mode, CORS); resource configuration
(projects/sources/providers) is DB-backed and API-mutable per plan §4.3.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["none", "hmac", "oidc"]


class ApiSettings(BaseSettings):
    """Environment-driven settings for the Control Plane API process."""

    model_config = SettingsConfigDict(env_prefix="HARBORRAG_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000
    env: Literal["dev", "prod"] = "dev"
    cors_origins: list[str] = []
    auth_mode: AuthMode = "none"
    auth_secret: str = ""
    docs_enabled: bool = True
