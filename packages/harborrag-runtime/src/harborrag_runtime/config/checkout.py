"""Derive host-side connection settings from a repository checkout's Compose env files.

The Compose stack reads ``env/.env.database`` itself; a process on the host (the
MCP checkout launcher, the operator CLI) has to translate the same values into
the ``HARBORRAG_*`` settings it would receive in a container. This is the one
place that translation lives, so both agree on quoting (python-dotenv follows
the same rules Compose applies) and on how the control-database URL is built.
"""

from __future__ import annotations

import re
from collections import ChainMap
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

DATABASE_ENV_FILE = Path("env/.env.database")
API_ENV_FILE = Path("env/.env.api")
OWNER_USER = "POSTGRES_USER"
OWNER_PASSWORD = "POSTGRES_PASSWORD"  # noqa: S105 - a variable name, not a value
CONTROL_DB_URL = "HARBORRAG_CONTROL_DB_URL"
SECRETS_ENCRYPTION_KEY = "HARBORRAG_SECRETS_ENCRYPTION_KEY"  # noqa: S105 - a variable name

# ``${NAME}`` and ``${NAME:-default}``: the two forms the env-example files use.
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


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


def checkout_object_store_credentials(path: Path) -> dict[str, str]:
    """Only the MinIO root pair, mapped to the object-store settings a host CLI reads.

    The rest of ``env/.env.database`` (Postgres passwords above all) is not mapped; the
    control database is only taken from it through an explicit ``HARBORRAG_CONTROL_DB_URL``
    line (see :func:`checkout_control_db_override`), together with the secrets key that
    database's stored secrets are encrypted under. A missing file, or one without both
    values, yields ``{}``.
    """

    if not path.is_file():
        return {}
    values = read_compose_env_file(path)
    user, password = values.get("MINIO_ROOT_USER"), values.get("MINIO_ROOT_PASSWORD")
    if not user or not password:
        return {}
    return {
        "HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID": user,
        "HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY": password,
    }


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


def resolve_compose_references(value: str, lookup: Mapping[str, str], *, label: str) -> str:
    """Substitute ``${NAME}``/``${NAME:-default}`` in ``value`` from ``lookup``, verbatim.

    Raises ValueError naming ``label`` and every reference that resolves to nothing, so a
    caller never builds a URL with an empty user or database. Values are never echoed.
    """

    unresolved: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        resolved = lookup.get(name) or default
        if resolved is None:
            unresolved.append(name)
            return ""
        return resolved

    resolved = _REFERENCE.sub(substitute, value)
    if unresolved:
        raise ValueError(f"{label} references unset variables: " + ", ".join(unresolved))
    return resolved


def checkout_control_db_override(path: Path, environ: Mapping[str, str]) -> str | None:
    """The ``HARBORRAG_CONTROL_DB_URL`` line of ``path``, interpolated; None without one.

    The line is opt-in: a file that only holds the ``POSTGRES_*`` values never points a
    host process at the stack's database. References resolve as Compose resolves them,
    ``environ`` first and then the file, so the line can reuse the file's credentials.
    Substitution is verbatim, not URL-quoted. Raises ValueError naming any reference that
    resolves to nothing, rather than yielding a URL with an empty user or database.
    """

    if not path.is_file():
        return None
    values = read_compose_env_file(path)
    template = values.get(CONTROL_DB_URL, "").strip()
    if not template:
        return None
    return resolve_compose_references(
        template, ChainMap(dict(environ), values), label=f"{CONTROL_DB_URL} in {path}"
    )


def checkout_secrets_encryption_key(path: Path) -> str | None:
    """The stack's ``HARBORRAG_SECRETS_ENCRYPTION_KEY`` from ``path``; None when blank.

    A host process that uses the file's control-database line reads secrets the API and
    worker stored under this key, so the two must travel together.
    """

    if not path.is_file():
        return None
    key = read_compose_env_file(path).get(SECRETS_ENCRYPTION_KEY, "")
    return key if key.strip() else None


def checkout_environment_name(root: Path) -> str | None:
    """``HARBORRAG_ENV`` as the checkout's API runs with, or None when unset."""

    path = root / API_ENV_FILE
    if not path.is_file():
        return None
    value = read_compose_env_file(path).get("HARBORRAG_ENV", "").strip()
    return value or None


__all__ = [
    "API_ENV_FILE",
    "CONTROL_DB_URL",
    "DATABASE_ENV_FILE",
    "OWNER_PASSWORD",
    "OWNER_USER",
    "SECRETS_ENCRYPTION_KEY",
    "checkout_control_db_override",
    "checkout_control_db_url",
    "checkout_environment_name",
    "checkout_secrets_encryption_key",
    "control_db_url_from_compose",
    "missing_control_db_values",
    "read_compose_env_file",
    "resolve_compose_references",
]
