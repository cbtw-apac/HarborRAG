"""Configuration and environment checks: optional clients, catalogs, credentials."""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

from pydantic import ValidationError

from harborrag_runtime.config import ConnectorCatalog, describe_model_catalog

from .checks import Check

_MODELS_HINT = "Set the provider key in .env (it is referenced as ${VAR} in config/models.yaml)."


_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# Import name -> distribution name for the clients the local stack needs. A bare
# `pip install harborrag` deliberately ships none of them.
_LOCAL_STACK_CLIENTS: tuple[tuple[str, str], ...] = (
    ("qdrant_client", "qdrant-client"),
    ("falkordb", "falkordb"),
    ("aioboto3", "aioboto3"),
    ("litellm", "litellm"),
    ("sqlalchemy", "SQLAlchemy"),
    ("alembic", "alembic"),
)


_PACKAGES_HINT = 'Install the local stack clients: pip install "harborrag[local]"'


def packages_check() -> Check:
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


def credentials_check(catalog: ConnectorCatalog) -> Check | None:
    """Every URL/secret variable a remote connector references must be non-empty."""

    blank: list[str] = []
    remote = 0
    for definition in catalog.connectors.values():
        if not definition.enabled or definition.provider == "local":
            continue
        remote += 1
        references = (
            *definition.setting_environment.values(),
            *definition.secret_environment.values(),
        )
        blank.extend(
            f"{definition.name}: {variable}"
            for variable in references
            if not os.environ.get(variable, "").strip()
        )
    if not remote:
        return None
    if blank:
        detail = "blank: " + ", ".join(sorted(set(blank)))
        return Check(
            "connector credentials",
            "environment",
            "fail",
            detail,
            hint="Set the variables named above in .env.",
        )
    return Check(
        "connector credentials", "environment", "ok", f"{remote} remote connector(s) configured"
    )


def models_check(path: Path) -> Check:
    try:
        summary = describe_model_catalog(path)
    except Exception as exc:  # noqa: BLE001
        return Check("models catalog", "config", "fail", str(exc), hint=_MODELS_HINT)
    # The loader expands ``${VAR}`` eagerly but accepts an empty value, which would only
    # surface as an authentication failure mid-ingestion. Name the blank variable now.
    blank = sorted(
        {
            name
            for name in _ENV_REFERENCE.findall(path.read_text(encoding="utf-8"))
            if not os.environ.get(name, "").strip()
        }
    )
    if blank:
        detail = f"{', '.join(blank)} is blank"
        return Check("models catalog", "config", "fail", detail, hint=_MODELS_HINT)
    detail = f"chat={summary.chat_default} embed={summary.embed_default}"
    return Check("models catalog", "config", "ok", detail)


def source_path_checks(catalog: ConnectorCatalog) -> list[Check]:
    checks: list[Check] = []
    for definition in catalog.connectors.values():
        variable = dict(definition.setting_environment).get("source_path")
        if not definition.enabled or definition.provider != "local" or variable is None:
            continue
        value = os.environ.get(variable, "")
        if not value:
            hint = f"Set {variable} in .env to the folder to ingest."
            checks.append(
                Check("source path", "environment", "fail", f"{variable} is not set", hint=hint)
            )
        elif not Path(value).expanduser().is_dir():
            # Never echo the value: a catalog can name any variable here.
            detail = f"{variable} does not point to an existing directory"
            hint = f"Check {variable} in .env."
            checks.append(Check("source path", "environment", "fail", detail, hint=hint))
        else:
            checks.append(Check("source path", "environment", "ok", f"{variable}={value}"))
    return checks


__all__ = [
    "credentials_check",
    "models_check",
    "packages_check",
    "settings_error_detail",
    "source_path_checks",
]
