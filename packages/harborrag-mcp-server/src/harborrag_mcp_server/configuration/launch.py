"""Checkout environment adapter for the MCP command.

Installed deployments provide HARBORRAG_* settings directly. The checkout
wrapper uses this adapter to read local Compose env files without executing
their contents as shell code or duplicating backend defaults in Bash.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping, Sequence
from pathlib import Path
from urllib.parse import quote

from dotenv.parser import parse_stream


def _read_env_file(path: Path) -> dict[str, str]:
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


def _from_root(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _set_default(environ: MutableMapping[str, str], name: str, value: str | None) -> None:
    if value is not None and name not in environ:
        environ[name] = value


def _checkout_defaults(root: Path, environ: MutableMapping[str, str], *, check: bool) -> None:
    database_values = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    if "HARBORRAG_CONTROL_DB_URL" not in environ:
        missing = [name for name in database_values if not environ.get(name)]
        if missing and not check:
            raise ValueError("Checkout database configuration is missing: " + ", ".join(missing))
        if not missing:
            user = quote(environ["POSTGRES_USER"], safe="")
            password = quote(environ["POSTGRES_PASSWORD"], safe="")
            database = quote(environ["POSTGRES_DB"], safe="")
            port = environ.get("POSTGRES_PORT", "5432")
            _set_default(
                environ,
                "HARBORRAG_CONTROL_DB_URL",
                f"postgresql+asyncpg://{user}:{password}@localhost:{port}/{database}",
            )

    _set_default(
        environ,
        "HARBORRAG_OBJECT_STORE_ENDPOINT_URL",
        f"http://localhost:{environ.get('MINIO_API_PORT', '9000')}",
    )
    _set_default(environ, "HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID", environ.get("MINIO_ROOT_USER"))
    _set_default(
        environ, "HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY", environ.get("MINIO_ROOT_PASSWORD")
    )
    _set_default(
        environ,
        "HARBORRAG_QDRANT_URL",
        f"http://localhost:{environ.get('QDRANT_HTTP_PORT', '6333')}",
    )
    _set_default(environ, "HARBORRAG_FALKORDB_HOST", "localhost")
    _set_default(environ, "HARBORRAG_FALKORDB_PORT", environ.get("FALKORDB_PORT", "6379"))
    _set_default(environ, "HARBORRAG_MODEL_CONFIG_PATH", str(root / "config/models.yaml"))
    _set_default(environ, "HARBORRAG_MCP_CONFIG_PATH", str(root / "config/mcp.yaml"))
    _set_default(environ, "HARBORRAG_MCP_KEYS_PATH", str(root / "config/mcp_keys.yaml"))

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


def load_launch_environment(
    *,
    checkout_root: Path | None,
    env_files: Sequence[Path] = (),
    check: bool = False,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Load data files with process env precedence and checkout-only adapters."""
    target = os.environ if environ is None else environ
    protected = set(target)
    pending = dict(target)
    files: list[Path] = []
    root = checkout_root.resolve() if checkout_root is not None else None
    if root is not None:
        if not root.is_dir():
            raise ValueError(f"Checkout root does not exist: {root}")
        for variable, default in (
            ("DATABASE_ENV_FILE", "env/.env.database"),
            ("MODEL_ENV_FILE", "env/.env.models"),
        ):
            files.append(_from_root(root, target.get(variable, default)))
        mcp_file = _from_root(root, target.get("MCP_ENV_FILE", "env/.env.mcp"))
        if mcp_file.is_file():
            files.append(mcp_file)
    files.extend(env_files)
    for path in files:
        for name, value in _read_env_file(path).items():
            if name not in protected:
                pending[name] = value
    if root is not None:
        _checkout_defaults(root, pending, check=check)
    target.update(pending)
