"""Checkout environment adapter for the MCP command.

Installed deployments provide HARBORRAG_* settings directly. The checkout
wrapper uses this adapter to read local Compose env files without executing
their contents as shell code or duplicating backend defaults in Bash.
The server it configures connects with its read-only accounts, never with the
Postgres owner or the MinIO root credentials of ``env/.env.database``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path

from harborrag_runtime.config.checkout import (
    CONTROL_DB_URL,
    control_db_url_from_compose,
    missing_control_db_values,
    read_compose_env_file,
    resolve_compose_references,
)

READER_DATABASE_ROLE = "harborrag_mcp"
READER_DATABASE_PASSWORD = "POSTGRES_MCP_PASSWORD"  # noqa: S105 - a variable name, not a value
READER_OBJECT_STORE_USER = "harborrag-mcp"
READER_OBJECT_STORE_PASSWORD = "MINIO_MCP_PASSWORD"  # noqa: S105 - a variable name, not a value
_READER_ROLE_KEY = "harborrag_mcp role"
_ACCESS_HINT = "; 'scripts/deployment/dev.sh mcp-access' sets the MCP passwords"
_OBJECT_STORE_SECRET = "HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY"  # noqa: S105 - a setting name


def _from_root(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _set_default(environ: MutableMapping[str, str], name: str, value: str | None) -> None:
    if value is not None and name not in environ:
        environ[name] = value


def _with_reader_role(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the values plus the read-only role's name, under a key no env file can set."""
    return {**environ, _READER_ROLE_KEY: READER_DATABASE_ROLE}


def _set_reader_database(environ: MutableMapping[str, str], *, check: bool) -> None:
    """Point the control-database URL at the read-only role, unless one is set.

    Raises:
        ValueError: Serving, and the password or the database name is missing.
    """
    if CONTROL_DB_URL in environ:
        return
    values = _with_reader_role(environ)
    names = {"user_name": _READER_ROLE_KEY, "password_name": READER_DATABASE_PASSWORD}
    missing = missing_control_db_values(values, **names)
    if missing and not check:
        raise ValueError(
            "Checkout database configuration is missing: " + ", ".join(missing) + _ACCESS_HINT
        )
    if not missing:
        environ[CONTROL_DB_URL] = control_db_url_from_compose(values, **names)


def _set_reader_object_store(environ: MutableMapping[str, str], *, check: bool) -> None:
    """Give the object store the read-only user's keys, unless a secret key is set.

    Raises:
        ValueError: Serving, and the read-only user's secret key is missing.
    """
    if _OBJECT_STORE_SECRET in environ:
        return
    secret = environ.get(READER_OBJECT_STORE_PASSWORD)
    if not secret:
        if not check:
            raise ValueError(
                "Checkout object store configuration is missing: "
                + READER_OBJECT_STORE_PASSWORD
                + _ACCESS_HINT
            )
        return
    _set_default(environ, "HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID", READER_OBJECT_STORE_USER)
    environ[_OBJECT_STORE_SECRET] = secret


def _checkout_defaults(root: Path, environ: MutableMapping[str, str], *, check: bool) -> None:
    _set_reader_database(environ, check=check)
    _set_default(
        environ,
        "HARBORRAG_OBJECT_STORE_ENDPOINT_URL",
        f"http://localhost:{environ.get('MINIO_API_PORT', '9000')}",
    )
    _set_reader_object_store(environ, check=check)
    _set_default(
        environ,
        "HARBORRAG_QDRANT_URL",
        f"http://localhost:{environ.get('QDRANT_HTTP_PORT', '6333')}",
    )
    _set_default(environ, "HARBORRAG_FALKORDB_HOST", "localhost")
    _set_default(environ, "HARBORRAG_FALKORDB_PORT", environ.get("FALKORDB_PORT", "6379"))
    _set_default(environ, "HARBORRAG_MODEL_CONFIG_PATH", str(root / "config/models/models.yaml"))
    _set_default(environ, "HARBORRAG_MCP_CONFIG_PATH", str(root / "config/mcp/server.yaml"))
    _set_default(environ, "HARBORRAG_MCP_KEYS_PATH", str(root / "config/mcp/keys.yaml"))

    # Local env files use paths relative to the checkout, even when a client
    # launches the wrapper from another working directory.
    for name in (
        "HARBORRAG_MODEL_CONFIG_PATH",
        "HARBORRAG_MCP_CONFIG_PATH",
        "HARBORRAG_MCP_KEYS_PATH",
        "HARBORRAG_MCP_AUDIT_PATH",
    ):
        if value := environ.get(name):
            environ[name] = str(_from_root(root, value))


def _resolve_control_db_line(environ: MutableMapping[str, str], *, check: bool) -> None:
    """Interpolate a file-supplied HARBORRAG_CONTROL_DB_URL against the merged values.

    env/.env.mcp or an ``--env-file`` may carry a line written with ${POSTGRES_*}
    references; serving the literal template would fail at connect time. An empty line
    falls back to the derived read-only URL.
    """

    if CONTROL_DB_URL not in environ:
        return
    try:
        url = resolve_compose_references(environ[CONTROL_DB_URL], environ, label=CONTROL_DB_URL)
    except ValueError:
        # A catalog check never opens the database; serving must name the gap.
        if not check:
            raise
        url = ""
    if url.strip():
        environ[CONTROL_DB_URL] = url
    else:
        del environ[CONTROL_DB_URL]


def load_launch_environment(
    *,
    checkout_root: Path | None,
    env_files: Sequence[Path] = (),
    check: bool = False,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Load data files with process env precedence and checkout-only adapters.

    The checkout's database env file never supplies HARBORRAG_CONTROL_DB_URL: its line
    is the host CLI's owner URL, and the server reads with its own role.
    """
    target = os.environ if environ is None else environ
    protected = set(target)
    pending = dict(target)
    files: list[Path] = []
    database_file: Path | None = None
    root = checkout_root.resolve() if checkout_root is not None else None
    if root is not None:
        if not root.is_dir():
            raise ValueError(f"Checkout root does not exist: {root}")
        database_file = _from_root(root, target.get("DATABASE_ENV_FILE", "env/.env.database"))
        files.append(database_file)
        files.append(_from_root(root, target.get("MODEL_ENV_FILE", "env/.env.models")))
        mcp_file = _from_root(root, target.get("MCP_ENV_FILE", "env/.env.mcp"))
        if mcp_file.is_file():
            files.append(mcp_file)
    files.extend(env_files)
    for path in files:
        for name, value in read_compose_env_file(path).items():
            if name in protected or (path == database_file and name == CONTROL_DB_URL):
                continue
            pending[name] = value
    if CONTROL_DB_URL not in protected:
        _resolve_control_db_line(pending, check=check)
    if root is not None:
        _checkout_defaults(root, pending, check=check)
    target.update(pending)
