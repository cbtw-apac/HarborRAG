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

from harborrag_runtime.config.checkout import (
    CONTROL_DB_URL,
    LOCAL_SOURCE_ROOT,
    SECRETS_ENCRYPTION_KEY,
    checkout_control_db_override,
    checkout_corpus_mode,
    checkout_local_source_root,
    checkout_object_store_credentials,
    checkout_secrets_encryption_key,
    checkout_tenant_id,
)
from harborrag_runtime.config.settings import DEFAULT_CONTROL_DB_URL

logger = logging.getLogger("harborrag.app.cli.environment")

# Mirrors the files scripts/deployment/dev.sh manages and compose mounts into the worker,
# and honours the same override variables so a non-default layout stays consistent
# between the CLI and the containers. env/.env.api and env/.env.database are deliberately
# not loaded wholesale: they point services at in-cluster hostnames and at the API's own
# control-plane DSN, which would silently repoint a host CLI at addresses it cannot reach.
# From env/.env.database only the MinIO root pair (the host reaches the same MinIO on
# localhost), HARBORRAG_LOCAL_SOURCE_ROOT, and an explicit HARBORRAG_CONTROL_DB_URL line,
# with the secrets key that database's secrets are stored under, are read (see load_project_environment); the
# POSTGRES_* values alone never select the database. From env/.env.api only the two
# corpus-mode lines are read, so chat on the host reads the corpus in the mode the API does,
# and its HARBORRAG_TENANT_ID, which stack_tenant_id() keeps out of the environment so a
# profile's tenant still outranks it.
_ENV_FILES: tuple[tuple[str, str], ...] = (
    ("CONNECTOR_ENV_FILE", "env/.env.connector"),
    ("PARSER_ENV_FILE", "env/.env.parser"),
    ("MODEL_ENV_FILE", "env/.env.models"),
)


_stack_tenant_id: str | None = None


def stack_tenant_id() -> str | None:
    """The tenant the stack's ``env/.env.api`` names, as ``load_project_environment`` last saw it."""
    return _stack_tenant_id


def load_project_environment(root: Path | None = None) -> tuple[Path, ...]:
    """Load the connector, parser and model env files, and selected keys of two stack files.

    From the database and API env files only the keys named at the top of this module are
    read. An exported variable always outranks a file. The API file's tenant is not exported:
    it is only recorded for :func:`stack_tenant_id`.

    Returns:
        The files that supplied at least one value.
    """

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
    if database_path is not None and _apply_database_values(database_path):
        loaded.append(database_path)
    api_path = _api_env_file(base)
    _record_stack_tenant(api_path)
    if api_path is not None and _apply_corpus_mode(api_path):
        logger.info("Applied the corpus mode from %s", api_path)
        loaded.append(api_path)
    elif _stack_tenant_id is not None and api_path is not None:
        loaded.append(api_path)
    if loaded:
        logger.debug("Loaded project environment files: %s", ", ".join(str(p) for p in loaded))
    return tuple(loaded)


def _record_stack_tenant(path: Path | None) -> None:
    """Remember the tenant ``path`` names, or forget the last one when it names none."""

    global _stack_tenant_id  # noqa: PLW0603 - one environment load per process, by design
    try:
        _stack_tenant_id = None if path is None else checkout_tenant_id(path)
    except (ValueError, OSError):
        logger.debug("Ignoring unreadable %s", path)
        _stack_tenant_id = None


def _apply_database_values(path: Path) -> bool:
    """Export the MinIO pair, control DB line and local source root of ``path``; True if any."""

    applied = False
    if _apply_object_store_credentials(path):
        logger.info("Applied the MinIO credentials from %s", path)
        applied = True
    if _apply_control_db_url(path):
        logger.info("Applied %s from %s", CONTROL_DB_URL, path)
        applied = True
    if _apply_local_source_root(path):
        logger.info("Applied %s from %s", LOCAL_SOURCE_ROOT, path)
        applied = True
    return applied


def _database_env_file(base: Path) -> Path | None:
    """The env/.env.database whose MinIO pair the CLI may use, if any."""

    return _checkout_env_file(base, "DATABASE_ENV_FILE", "env/.env.database")


def _api_env_file(base: Path) -> Path | None:
    """The env/.env.api whose corpus-mode lines the CLI may use, if any."""

    return _checkout_env_file(base, "API_ENV_FILE", "env/.env.api")


def _checkout_env_file(base: Path, override: str, relative: str) -> Path | None:
    """The stack file ``relative`` the CLI may read selected keys from, if any.

    Only a repo checkout runs the dev stack that file describes; any other directory
    that happens to hold one is not that stack. An explicit ``override`` variable, named as
    dev.sh names it, is the operator's choice and is honoured anywhere.
    """

    explicit = os.environ.get(override, "").strip()
    if explicit:
        chosen = Path(explicit)
        return chosen if chosen.is_absolute() else base / chosen
    from harborrag_app.cli.project import UnsafeProjectError, active_project, find_legacy_checkout

    if active_project() is not None:
        return None
    try:
        checkout = find_legacy_checkout(base)
    except UnsafeProjectError:
        return None
    # The checkout's own file: base may be a directory below the checkout root.
    return None if checkout is None else checkout / relative


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


def _apply_corpus_mode(path: Path) -> bool:
    """Export the file's corpus-mode lines where unset; True when anything was set."""

    try:
        lines = checkout_corpus_mode(path)
    except (ValueError, OSError):
        logger.debug("Ignoring unreadable %s", path)
        return False
    applied = False
    for name, value in lines.items():
        if name not in os.environ:
            os.environ[name] = value
            applied = True
    return applied


def _apply_control_db_url(path: Path) -> bool:
    """Export the file's ``HARBORRAG_CONTROL_DB_URL`` line where unset; True when set.

    Its ``HARBORRAG_SECRETS_ENCRYPTION_KEY`` comes along, again only where unset.
    """

    # An exported value -- even an empty one -- is the operator being explicit.
    if CONTROL_DB_URL in os.environ:
        return False
    try:
        url = checkout_control_db_override(path, os.environ)
    except ValueError as exc:
        # The message names the file and the unset variables, never a value.
        logger.warning("%s; using the default control DB", exc)
        return False
    except OSError:
        logger.debug("Ignoring unreadable %s", path)
        return False
    if url is None:
        return False
    os.environ[CONTROL_DB_URL] = url
    # The stack's Postgres holds secrets the API and worker encrypted under the file's
    # key, and the runtime refuses a non-sqlite control DB without one.
    if SECRETS_ENCRYPTION_KEY not in os.environ:
        try:
            key = checkout_secrets_encryption_key(path)
        except (ValueError, OSError):
            key = None
        if key is not None:
            os.environ[SECRETS_ENCRYPTION_KEY] = key
            logger.info("Applied %s from %s", SECRETS_ENCRYPTION_KEY, path)
    return True


def _apply_local_source_root(path: Path) -> bool:
    """Export the file's ``HARBORRAG_LOCAL_SOURCE_ROOT`` where unset; True when set."""

    if LOCAL_SOURCE_ROOT in os.environ:
        return False
    try:
        root = checkout_local_source_root(path)
    except (ValueError, OSError):
        logger.debug("Ignoring unreadable %s", path)
        return False
    if root is None:
        return False
    os.environ[LOCAL_SOURCE_ROOT] = root
    return True


_REFUSAL = (
    "Refusing to ingest or change connections from a repo checkout in the local sqlite "
    "control DB: the stores are shared with the stack but run state and connections would "
    "not be. Set HARBORRAG_CONTROL_DB_URL to "
    "the stack's Postgres, exported or as a line in env/.env.database (rebuild the images "
    "first: scripts/deployment/dev.sh up --build)."
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
    """Exit 1 with the refusal on stderr; the first statement of every ingest or source write."""

    message = checkout_write_refusal()
    if message is not None:
        typer.echo(message, err=True)
        raise typer.Exit(1)
