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
    database = Path(os.environ.get("DATABASE_ENV_FILE", "env/.env.database"))
    database_path = database if database.is_absolute() else base / database
    try:
        credentials = checkout_object_store_credentials(database_path)
    except ValueError:
        logger.debug("Ignoring unreadable %s", database_path)
        credentials = {}
    applied = False
    for name, value in credentials.items():
        # setdefault: a project's own .env or an exported variable outranks the checkout.
        if name not in os.environ:
            os.environ[name] = value
            applied = True
    if applied:
        loaded.append(database_path)
    if loaded:
        logger.debug("Loaded project environment files: %s", ", ".join(str(p) for p in loaded))
    return tuple(loaded)


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
    if configured and configured != DEFAULT_CONTROL_DB_URL:
        return None
    return _REFUSAL


def refuse_checkout_write() -> None:
    """Exit 1 with the refusal on stderr; the first statement of every ingest write."""

    message = checkout_write_refusal()
    if message is not None:
        typer.echo(message, err=True)
        raise typer.Exit(1)


__all__ = ["checkout_write_refusal", "load_project_environment", "refuse_checkout_write"]
