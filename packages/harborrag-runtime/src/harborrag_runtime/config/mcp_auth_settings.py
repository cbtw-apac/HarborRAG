"""MCP server key-authentication settings (``HARBORRAG_MCP_*``).

Every field is required: a server that cannot say how it authenticates, which
environment its keys belong to, or where its key store is must not start.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


def read_secret_file(path: Path) -> str:
    """Return a secret file's stripped contents; an empty file is an error."""

    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Secret file {path} is empty")
    return value


class McpAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HARBORRAG_MCP_", extra="ignore")

    auth_mode: Literal["api_key"]
    key_store: Literal["postgres"]
    environment: Literal["dev", "staging", "prod"]
    auth_database_dsn_file: Path

    def read_database_dsn(self) -> str:
        return read_secret_file(self.auth_database_dsn_file)


__all__ = ["McpAuthSettings", "read_secret_file"]
