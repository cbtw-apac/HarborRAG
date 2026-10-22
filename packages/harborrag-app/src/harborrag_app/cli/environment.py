"""Load the project's protected environment files into the CLI process.

The deployment already keeps connector credentials in ``env/.env.connector`` and model
credentials in ``env/.env.models``; compose hands those files to the worker and API
containers with ``env_file:``. Nothing did the equivalent for the CLI, which runs on the
host, so every command that resolves a connector or an embedding model had to be
prefixed with the same variables by hand even though the project already had them
configured.

Loading here keeps one source of truth. The real environment always wins, so an inline
prefix or an exported variable still overrides the file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer

from harborrag_runtime.config.checkout import checkout_object_store_credentials
from harborrag_runtime.config.settings import DEFAULT_CONTROL_DB_URL

logger = logging.getLogger("harborrag.app.cli.environment")

# Mirrors the files scripts/deployment/dev.sh manages and compose mounts into the worker,
# and honours the same override variables so a non-default layout stays consistent
# between the CLI and the containers. env/.env.api and env/.env.database are deliberately
# not loaded wholesale: they point services at in-cluster hostnames and at the API's own
# control-plane DSN, which would silently repoint a host CLI at addresses it cannot reach.
# Only the MinIO root pair is read from env/.env.database (see
# load_project_environment): the host reaches the same MinIO on localhost.
_ENV_FILES: tuple[tuple[str, str], ...] = (
    ("CONNECTOR_ENV_FILE", "env/.env.connector"),
    ("PARSER_ENV_FILE", "env/.env.parser"),
    ("MODEL_ENV_FILE", "env/.env.models"),
)


def load_project_environment(root: Path | None = None) -> tuple[Path, ...]:
    """Load the connector, parser, and model env files; return the ones applied."""

    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv ships with the CLI extras
        logger.debug("python-dotenv is unavailable; skipping project environment files")
        return ()
    base = root or Path.cwd()
    loaded: list[Path] = []
    for variable, default in _ENV_FILES:
        candidate = Path(os.environ.get(variable, default))
        path = candidate if candidate.is_absolute() else base / candidate
        if not path.is_file():
            continue
        # override=False: an operator who exports a variable, or prefixes one onto the
        # command, is being explicit and must outrank the file.
        load_dotenv(path, override=False)
        loaded.append(path)
    database_path = _database_env_file(base)
    if database_path is not None and _apply_object_store_credentials(database_path):
        logger.info("Applied the MinIO credentials from %s", database_path)
        loaded.append(database_path)
    if loaded:
        logger.debug("Loaded project environment files: %s", ", ".join(str(p) for p in loaded))
    return tuple(loaded)


def _database_env_file(base: Path) -> Path | None:
    """The env/.env.database whose MinIO pair the CLI may use, if any.

    Only a repo checkout runs the dev stack that file describes; a ``harborrag init``
    project, or any other directory, that happens to hold one is not that stack. An
    explicit ``DATABASE_ENV_FILE`` is the operator's choice and is honoured anywhere.
    """

    explicit = os.environ.get("DATABASE_ENV_FILE", "").strip()
    if explicit:
        database = Path(explicit)
        return database if database.is_absolute() else base / database
    from harborrag_app.cli.project import UnsafeProjectError, active_project, find_legacy_checkout

    if active_project() is not None:
        return None
    try:
        checkout = find_legacy_checkout(base)
    except UnsafeProjectError:
        return None
    # The checkout's own file: base may be a directory below the checkout root.
    return None if checkout is None else checkout / "env/.env.database"


def _apply_object_store_credentials(path: Path) -> bool:
    """Export the MinIO pair from ``path`` where unset; True when anything was set."""

    try:
        credentials = checkout_object_store_credentials(path)
    except (ValueError, OSError):
        logger.debug("Ignoring unreadable %s", path)
        return False
    applied = False
    for name, value in credentials.items():
        # setdefault: a project's own .env or an exported variable outranks the checkout.
        if name not in os.environ:
            os.environ[name] = value
            applied = True
    return applied


_REFUSAL = (
    "Refusing to ingest from a repo checkout into the local sqlite control DB: the stores "
    "are shared with the stack but run state would not be. Set HARBORRAG_CONTROL_DB_URL to "
    "the stack's Postgres (rebuild the images first: scripts/deployment/dev.sh up --build) "
    "or run from a project created with `harborrag init`."
)


def checkout_write_refusal() -> str | None:
    """Why a checkout may not ingest into the default sqlite control DB, if it may not."""

    from harborrag_app.cli.project import UnsafeProjectError, active_project, find_legacy_checkout

    if active_project() is not None:
        return None
    try:
        if find_legacy_checkout() is None:
            return None
    except UnsafeProjectError:
        return None
    configured = os.environ.get("HARBORRAG_CONTROL_DB_URL")
    if configured and not _is_default_control_db(configured):
        return None
    return _REFUSAL


def _sqlite_file(url: str) -> Path | None:
    """The database file a sqlite URL names, resolved against the CWD; else None.

    A hand parse: sqlalchemy's URL parser would load sqlalchemy into every CLI start.
    """

    scheme, separator, rest = url.partition(":///")
    if not separator or not scheme.lower().startswith("sqlite"):
        return None
    database = rest.split("?", 1)[0]
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def _is_default_control_db(url: str) -> bool:
    """Whether ``url`` is the default sqlite control DB, however it is spelled."""

    if url == DEFAULT_CONTROL_DB_URL:
        return True
    configured = _sqlite_file(url)
    return configured is not None and configured == _sqlite_file(DEFAULT_CONTROL_DB_URL)


def refuse_checkout_write() -> None:
    """Exit 1 with the refusal on stderr; the first statement of every ingest write."""

    message = checkout_write_refusal()
    if message is not None:
        typer.echo(message, err=True)
        raise typer.Exit(1)


__all__ = ["checkout_write_refusal", "load_project_environment", "refuse_checkout_write"]
