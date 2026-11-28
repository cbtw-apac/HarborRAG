"""Configuration and environment checks: optional clients, catalogs, credentials."""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from harborrag_app.cli.globals import TENANT_ENV
from harborrag_app.cli.project import active_project
from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_runtime.application.errors import public_error_message, validation_error_detail
from harborrag_runtime.application.retrieval.access import corpus_mode_for
from harborrag_runtime.config import ConnectorCatalog, describe_model_catalog
from harborrag_runtime.config.connectors.models.local_file import (
    LOCAL_SOURCE_ROOT_VARIABLE,
    local_source_root,
)
from harborrag_runtime.config.models import has_rerank_model, require_set_model_variables
from harborrag_runtime.config.settings import RuntimeSettings

from .checks import Check

logger = logging.getLogger("harborrag.app.cli.doctor.environment")


def env_hint(kind: Literal["models", "connector", "database", "services", "settings"]) -> str:
    """The file (or command) that supplies ``kind`` in the current mode.

    A project directory from an earlier release keeps everything in its ``.env`` and its
    compose file. A repository checkout, the supported setup, splits the same values across
    ``env/.env.*`` and starts services with dev.sh; outside both, the hint names the
    checkout's files too.
    """
    if active_project() is not None:
        return "docker compose up -d" if kind == "services" else ".env"
    return {
        "models": "env/.env.models",
        "connector": "env/.env.connector",
        "database": "env/.env.database",
        "services": "scripts/deployment/dev.sh data",
        "settings": "your shell environment",
    }[kind]


def _models_hint() -> str:
    return (
        f"Set the provider key in {env_hint('models')} "
        "(it is referenced as ${VAR} in config/models/models.yaml)."
    )


_LOCAL_STACK_CLIENTS: tuple[tuple[str, str], ...] = (
    ("qdrant_client", "qdrant-client"),
    ("falkordb", "falkordb"),
    ("aiobotocore", "aiobotocore"),
    ("litellm", "litellm"),
    ("sqlalchemy", "SQLAlchemy"),
    ("alembic", "alembic"),
)
"""Import name -> distribution name for the clients the local stack needs. A bare `pip install
harborrag` deliberately ships none of them.
"""


_PACKAGES_HINT = 'Install the local stack clients: pip install "harborrag[local]"'


def packages_check() -> Check:
    """Check that the local-stack client libraries are installed."""
    missing = [
        distribution
        for module, distribution in _LOCAL_STACK_CLIENTS
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        detail = f"missing: {', '.join(missing)}"
        return Check("python packages", "environment", "fail", detail, hint=_PACKAGES_HINT)
    return Check("python packages", "environment", "ok", "local stack clients importable")


def settings_error_detail(exc: Exception) -> str:
    """Name the offending settings without echoing their values.

    ``str(ValidationError)`` appends a truncated ``input_value={...}`` dump of the raw
    environment, whose tail can be a credential; report locations and messages only.
    """
    if not isinstance(exc, ValidationError):
        return f"{type(exc).__name__}: {exc}"
    parts = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(item) for item in error.get("loc", ()))
        name = f"HARBORRAG_{location.upper()}" if location else "settings"
        parts.append(f"{name}: {error.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid runtime settings"


def check_error_detail(exc: Exception) -> str:
    """Describe a failed check without echoing expanded configuration values.

    ``config/models/models.yaml`` interpolates ``${VAR}`` eagerly, so a pydantic error's
    ``input_value={...}`` dump can carry the provider key, and a composition failure can
    carry HARBORRAG_CONTROL_DB_URL with its password. Neither may be forwarded.

    ``HarborConfigurationError`` is the exception: all of its messages are authored in
    this repository, name HARBORRAG_* variables rather than their values, and are the
    operator's whole diagnostic -- the migration-skew message above all. It is forwarded
    here rather than added to ``_PUBLIC_MESSAGE_TYPES`` because that allowlist also feeds
    HTTP responses and would admit every future subclass unreviewed.

    ValidationError is a ValueError, so it must be matched before the allowlist forwards
    ValueError verbatim.
    """
    if isinstance(exc, ValidationError):
        return validation_error_detail(exc)
    if isinstance(exc, HarborConfigurationError):
        return f"{type(exc).__name__}: {exc}"
    return public_error_message(exc)


def credentials_check(catalog: ConnectorCatalog) -> Check | None:
    """Warn about enabled remote connectors whose URL or secret variables are blank.

    A fresh checkout ships remote connectors with blank credentials, and a local run does
    not need them, so this reports the connectors that cannot sync yet and never fails.
    """
    unconfigured: dict[str, list[str]] = {}
    remote = 0
    for definition in catalog.connectors.values():
        if not definition.enabled or definition.provider == "local":
            continue
        remote += 1
        references = (
            *definition.setting_environment.values(),
            *definition.secret_environment.values(),
        )
        blank = sorted(
            {variable for variable in references if not os.environ.get(variable, "").strip()}
        )
        if blank:
            unconfigured[definition.name] = blank
    if not remote:
        return None
    if unconfigured:
        detail = f"blank credentials for: {', '.join(sorted(unconfigured))}"
        variables = "; ".join(
            f"{name}: {', '.join(blank)}" for name, blank in sorted(unconfigured.items())
        )
        hint = f"Set these in {env_hint('connector')} before syncing the connector: {variables}."
        return Check(
            "connector credentials", "environment", "warn", detail, hint=hint, required=False
        )
    return Check(
        "connector credentials", "environment", "ok", f"{remote} remote connector(s) configured"
    )


def models_check(path: Path) -> Check:
    """Check that the model catalog loads.

    The loader expands ``${VAR}`` eagerly but accepts an empty value, which would only surface as an
    authentication failure mid-ingestion. Name the blank variable now. Scan the parsed values, not
    the raw text: a `${VAR}` in a commented-out example section is never expanded, so it must not
    fail the check. safe_load is laxer than the model loader, which refuses duplicate keys and
    unknown sections, but it reads the same values once that loader has accepted the catalog.
    A named reranker whose key variable is empty fails too, as the MCP server and the API refuse
    it; a ``${VAR:-}`` default hides that reference from the blank-variable scan.

    The file can change or vanish between the reads.
    """
    try:
        summary = describe_model_catalog(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Model catalog %s could not be described", path, exc_info=True)
        detail = f"{path}: {check_error_detail(exc)}"
        return Check("models catalog", "config", "fail", detail, hint=_models_hint())
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a check reports, it never raises
        logger.debug("Model catalog %s could not be re-read", path, exc_info=True)
        detail = f"{path}: {check_error_detail(exc)}"
        return Check("models catalog", "config", "fail", detail, hint=_models_hint())
    try:
        require_set_model_variables(raw)
    except ValueError as exc:
        return Check("models catalog", "config", "fail", str(exc), hint=_models_hint())
    try:
        has_rerank_model(path)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        detail = f"{path}: {check_error_detail(exc)}"
        return Check("models catalog", "config", "fail", detail, hint=_models_hint())
    detail = f"chat={summary.chat_default} embed={summary.embed_default}"
    return Check("models catalog", "config", "ok", detail)


def source_root_check() -> Check:
    """Report the folder ``local_file`` connections must stay inside.

    Reads ``HARBORRAG_LOCAL_SOURCE_ROOT`` as the operations do. Unset is a warning: the CLI
    and ``connectors.yaml`` then accept any path, but the API refuses every ``local_file``
    connection. The value is never echoed. A root absent on this host is also a warning,
    since the API and workers resolve it in their own filesystem.
    """
    root = local_source_root()
    if root is None:
        detail = (
            f"{LOCAL_SOURCE_ROOT_VARIABLE} is unset; the CLI accepts any local_file path, "
            "the API refuses every local_file connection"
        )
        hint = (
            f"Set {LOCAL_SOURCE_ROOT_VARIABLE} in the API and worker environment, and for the "
            f"CLI on its line in {env_hint('database')}."
        )
        return Check("source path", "environment", "warn", detail, hint=hint, required=False)
    if not root.is_dir():
        detail = f"{LOCAL_SOURCE_ROOT_VARIABLE} does not name a directory on this host"
        hint = f"Fine when only the API and workers mount it; otherwise check {LOCAL_SOURCE_ROOT_VARIABLE}."
        return Check("source path", "environment", "warn", detail, hint=hint, required=False)
    detail = f"{LOCAL_SOURCE_ROOT_VARIABLE} points to an existing directory"
    return Check("source path", "environment", "ok", detail)


def corpus_mode_check(settings: RuntimeSettings, tenant_id: str) -> Check:
    """Report the corpus mode chat uses for ``tenant_id``; ``source_acl`` is a warning.

    Never a failure: ``source_acl`` is a valid, stricter mode, but a caller with no source
    permissions then gets no sources.
    """
    mode = corpus_mode_for(settings, tenant_id)
    detail = f"{mode} for tenant {tenant_id}"
    if mode == "tenant_shared":
        return Check("corpus mode", "config", "ok", detail)
    hint = (
        "Chat reads only sources the caller has permissions for; see workspace mode "
        "(docs/users/configuration/workspace-mode.md)."
    )
    shared = settings.corpus_shared_tenant_id
    if settings.corpus_access_mode == "tenant_shared" and shared:
        detail = f"{detail}; the shared corpus belongs to tenant {shared}"
        hint = f"Pass --tenant {shared} or set {TENANT_ENV}. {hint}"
    return Check("corpus mode", "config", "warn", detail, hint=hint, required=False)
