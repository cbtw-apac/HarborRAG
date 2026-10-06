"""Derive host-side connection settings from a repository checkout's Compose env files.

The Compose stack reads ``env/.env.database`` itself; a process on the host (the
MCP checkout launcher, the operator CLI) has to translate the same values into
the ``HARBORRAG_*`` settings it would receive in a container. This is the one
place that translation lives, so both agree on quoting (python-dotenv follows
the same rules Compose applies) and on how the control-database URL is built.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

DATABASE_ENV_FILE = Path("env/.env.database")
API_ENV_FILE = Path("env/.env.api")
OWNER_USER = "POSTGRES_USER"
OWNER_PASSWORD = "POSTGRES_PASSWORD"  # noqa: S105 - a variable name, not a value


def read_compose_env_file(path: Path) -> dict[str, str]:
    """Parse a Compose-style env file; quoted values are unquoted as Compose does."""

    from dotenv.parser import parse_stream

    if not path.is_file():
        raise ValueError(f"Environment file does not exist: {path}")
    values: dict[str, str] = {}
    with path.open(encoding="utf-8") as stream:
        for binding in parse_stream(stream):
            if binding.error:
                raise ValueError(f"Invalid environment file {path} at line {binding.original.line}")
            if binding.key is not None and binding.value is not None:
                values[binding.key] = binding.value
    return values


def missing_control_db_values(
    values: Mapping[str, str], *, user_name: str = OWNER_USER, password_name: str = OWNER_PASSWORD
) -> list[str]:
    return [name for name in (user_name, password_name, "POSTGRES_DB") if not values.get(name)]


def control_db_url_from_compose(
    values: Mapping[str, str],
    *,
    user_name: str = OWNER_USER,
    password_name: str = OWNER_PASSWORD,
    host: str = "localhost",
) -> str:
    """The asyncpg URL a host process uses to reach the Compose PostgreSQL.

    ``user_name``/``password_name`` select which credential pair to embed (the
    owner account, or the MCP reader role once provisioned). Raises ValueError
    naming the values that are missing.
    """

    missing = missing_control_db_values(values, user_name=user_name, password_name=password_name)
    if missing:
        raise ValueError("Checkout database configuration is missing: " + ", ".join(missing))
    user = quote(values[user_name], safe="")
    password = quote(values[password_name], safe="")
    database = quote(values["POSTGRES_DB"], safe="")
    port = values.get("POSTGRES_PORT", "5432")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"


def checkout_control_db_url(root: Path) -> str | None:
    """Owner-credential URL for a checkout, or None when it has no database env file."""

    path = root / DATABASE_ENV_FILE
    if not path.is_file():
        return None
    return control_db_url_from_compose(read_compose_env_file(path))


def checkout_environment_name(root: Path) -> str | None:
    """``HARBORRAG_ENV`` as the checkout's API runs with, or None when unset."""

    path = root / API_ENV_FILE
    if not path.is_file():
        return None
    value = read_compose_env_file(path).get("HARBORRAG_ENV", "").strip()
    return value or None


__all__ = [
    "API_ENV_FILE",
    "DATABASE_ENV_FILE",
    "OWNER_PASSWORD",
    "OWNER_USER",
    "checkout_control_db_url",
    "checkout_environment_name",
    "control_db_url_from_compose",
    "missing_control_db_values",
    "read_compose_env_file",
]
