"""API process settings, loaded from HARBORRAG_* environment variables (ST2).

Static process config only (ports, auth mode, CORS); resource configuration
(projects/sources/providers) is DB-backed and API-mutable per plan §4.3.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import model_validator
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

    @model_validator(mode="before")
    @classmethod
    def _default_docs_disabled_in_prod(cls, data: Any) -> Any:
        """Disable Swagger/OpenAPI docs by default in prod unless requested.

        ``docs_enabled`` defaults to True for local/dev ergonomics, but that
        default must not silently expose the API schema in production. If
        ``env`` resolves to "prod" and the operator never set
        ``docs_enabled``/``HARBORRAG_DOCS_ENABLED`` explicitly, this flips
        the effective default to False; an explicit value (either way) is
        always respected.
        """
        if isinstance(data, dict) and data.get("env") == "prod" and "docs_enabled" not in data:
            data = {**data, "docs_enabled": False}
        return data
