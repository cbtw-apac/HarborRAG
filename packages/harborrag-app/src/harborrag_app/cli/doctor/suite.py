"""Assemble the layered readiness checks: project, config, environment, services."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Callable

from harborrag_app.cli.project import PROJECT_FILE, ProjectError, active_project, find_project
from harborrag_app.workflow_control import BaseAppService
from harborrag_runtime.config import (
    ConnectorCatalog,
    load_connector_catalog,
    load_parser_catalog,
)
from harborrag_runtime.config.settings import RuntimeSettings

from . import probes
from .checks import Check, DoctorReport
from .environment import (
    credentials_check,
    models_check,
    packages_check,
    settings_error_detail,
    source_path_checks,
)

_COMPOSE_HINT = "Start the local services: docker compose up -d"
_INIT_HINT = "Run `harborrag init` to scaffold a project, or cd into one."
_TEMPORAL_HINT = (
    "Durable mode needs the Temporal stack (scripts/deployment/dev.sh up in a checkout)."
)
_TEMPORAL_CLIENT_HINT = (
    'Durable commands need the Temporal client: pip install "harborrag[temporal]"'
)


async def run_doctor(
    *,
    temporal: bool,
    service_factory: Callable[[], BaseAppService],
) -> DoctorReport:
    checks: list[Check] = [_project_check(), packages_check()]
    settings = _settings(checks)
    if settings is None:
        return DoctorReport(tuple(checks))
    checks.extend(_configuration_checks(settings))
    checks.extend(await asyncio.to_thread(_service_checks, settings))
    control_checks, diagnostics = await _control_plane_checks(service_factory, temporal=temporal)
    checks.extend(control_checks)
    return DoctorReport(tuple(checks), diagnostics=diagnostics)


def _project_check() -> Check:
    project = active_project()
    if project is None:
        try:
            project = find_project()
        except ProjectError as exc:
            return Check(
                "project",
                "project",
                "fail",
                str(exc),
                hint="Pass --project to use it deliberately.",
            )
    if project is None:
        return Check(
            "project",
            "project",
            "warn",
            f"no {PROJECT_FILE} found; using the current directory",
            hint=_INIT_HINT,
            required=False,
        )
    return Check("project", "project", "ok", str(project.root))


def _settings(checks: list[Check]) -> RuntimeSettings | None:
    try:
        return RuntimeSettings()
    except Exception as exc:  # noqa: BLE001 - reported as a check, never raised
        checks.append(
            Check(
                "runtime settings",
                "config",
                "fail",
                settings_error_detail(exc),
                hint="Fix the HARBORRAG_* variable named above in .env.",
            )
        )
        return None


def _configuration_checks(settings: RuntimeSettings) -> list[Check]:
    checks: list[Check] = []
    catalog: ConnectorCatalog | None = None
    try:
        catalog = load_connector_catalog(settings.connector_config_path)
        enabled = catalog.names(enabled_only=True)
        checks.append(
            Check(
                "connectors catalog",
                "config",
                "ok",
                f"{len(enabled)} enabled: {', '.join(enabled) or 'none'}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("connectors catalog", "config", "fail", str(exc), hint=_INIT_HINT))
    try:
        load_parser_catalog(settings.parser_config_path)
        checks.append(Check("parsers catalog", "config", "ok", str(settings.parser_config_path)))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("parsers catalog", "config", "fail", str(exc), hint=_INIT_HINT))
    checks.append(models_check(settings.model_config_path))
    if catalog is not None:
        checks.extend(source_path_checks(catalog))
        credentials = credentials_check(catalog)
        if credentials is not None:
            checks.append(credentials)
    return checks


def _service_checks(settings: RuntimeSettings) -> list[Check]:
    checks = [
        _service_check(
            "qdrant",
            settings.qdrant_url,
            probes.http_ok(settings.qdrant_url.rstrip("/") + "/readyz"),
        ),
        _service_check(
            "falkordb",
            f"{settings.falkordb_host}:{settings.falkordb_port}",
            probes.redis_ping(settings.falkordb_host, settings.falkordb_port),
        ),
    ]
    endpoint = settings.object_store_endpoint_url
    if endpoint is None:
        checks.append(
            Check("object store", "services", "skip", "no endpoint configured", required=False)
        )
        return checks
    error = probes.http_ok(endpoint.rstrip("/") + "/minio/health/live")
    if error is not None:
        # Not every S3-compatible store serves MinIO's health route; fall back to the port.
        host, port = probes.host_port(endpoint, default_port=9000)
        error = probes.tcp_reachable(host, port)
    checks.append(_service_check("object store", endpoint, error))
    return checks


def _service_check(name: str, target: str, error: str | None) -> Check:
    if error is None:
        return Check(name, "services", "ok", target)
    return Check(name, "services", "fail", error, hint=_COMPOSE_HINT)


async def _control_plane_checks(
    service_factory: Callable[[], BaseAppService],
    *,
    temporal: bool,
) -> tuple[list[Check], dict[str, object] | None]:
    """Control-plane and Temporal checks, plus the health diagnostics block if available."""

    try:
        service = await asyncio.to_thread(service_factory)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        hint = "Check HARBORRAG_CONTROL_DB_URL in .env."
        failed = [
            Check("control plane", "services", "fail", detail, hint=hint),
            _temporal_skipped(),
        ]
        return failed, None
    try:
        health = service.health()
        raw = health.data.get("diagnostics")
        diagnostics = dict(raw) if isinstance(raw, dict) else None
        checks = [
            Check(
                "control plane",
                "services",
                "ok" if health.ok else "fail",
                health.error or "ready",
            )
        ]
        if not temporal:
            checks.append(_temporal_skipped())
            return checks, diagnostics
        if importlib.util.find_spec("temporalio") is None:
            detail = "temporalio is not installed"
            checks.append(
                Check(
                    "temporal",
                    "durable",
                    "fail",
                    detail,
                    hint=_TEMPORAL_CLIENT_HINT,
                    required=False,
                )
            )
            return checks, diagnostics
        runtime = await service.runtime_health()
        checks.append(
            Check(
                "temporal",
                "durable",
                "ok" if runtime.ok else "fail",
                runtime.error or "ready",
                hint=_TEMPORAL_HINT,
                required=False,
            )
        )
        return checks, diagnostics
    finally:
        close = getattr(service, "aclose", None)
        if close is not None:
            await close()


def _temporal_skipped() -> Check:
    if importlib.util.find_spec("temporalio") is None:
        detail = (
            "client not installed; only needed for ingest start|status|watch|pause|resume|cancel"
        )
        return Check(
            "temporal", "durable", "skip", detail, hint=_TEMPORAL_CLIENT_HINT, required=False
        )
    return Check("temporal", "durable", "skip", "not checked; pass --temporal", required=False)


__all__ = ["run_doctor", "settings_error_detail"]
