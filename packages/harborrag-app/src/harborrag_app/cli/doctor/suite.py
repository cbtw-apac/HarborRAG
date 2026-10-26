"""Assemble the layered readiness checks: project, config, environment, services."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
from collections.abc import Callable
from pathlib import Path

from pydantic import SecretStr

from harborrag_app.cli.project import (
    PROJECT_FILE,
    ProjectError,
    UnsafeProjectError,
    active_project,
    find_legacy_checkout,
    find_project,
)
from harborrag_app.workflow_control import BaseAppService
from harborrag_runtime.config import (
    ConnectorCatalog,
    load_connector_catalog,
    load_parser_catalog,
)
from harborrag_runtime.config.settings import DEFAULT_CONTROL_DB_URL, RuntimeSettings

from . import probes
from .checks import Check, DoctorReport
from .environment import (
    check_error_detail,
    credentials_check,
    env_hint,
    models_check,
    packages_check,
    settings_error_detail,
    source_path_checks,
)

logger = logging.getLogger("harborrag.app.cli.doctor.suite")

_QDRANT_HTTP_PORT = 6333
_QDRANT_GRPC_PORT = 6334

_INIT_HINT = "Run `harborrag init` to scaffold a project, or cd into one."
_CONNECTORS_HINT = "Fix or remove the connector named above in config/connectors.yaml."
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
    control_db = _control_db_check(settings)
    if control_db is not None:
        checks.append(control_db)
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
                hint=f"Fix the HARBORRAG_* variable named above in {env_hint('settings')}.",
            )
        )
        return None


def _configuration_checks(settings: RuntimeSettings) -> list[Check]:
    checks: list[Check] = []
    catalog: ConnectorCatalog | None = None
    try:
        catalog = load_connector_catalog(settings.connector_config_path)
        enabled = catalog.names(enabled_only=True)
        detail = f"{len(enabled)} enabled: {', '.join(enabled) or 'none'}"
        # The loader keeps an invalid definition out of `connectors` and only raises
        # when it is looked up, so it would otherwise vanish from this report.
        if catalog.errors:
            invalid = "; ".join(
                f"{name}: {check_error_detail(error)}"
                for name, error in sorted(catalog.errors.items())
            )
            checks.append(
                Check(
                    "connectors catalog",
                    "config",
                    "fail",
                    f"{detail}; invalid: {invalid}",
                    hint=_CONNECTORS_HINT,
                )
            )
        else:
            checks.append(Check("connectors catalog", "config", "ok", detail))
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


def _control_db_check(settings: RuntimeSettings) -> Check | None:
    """Warn when a checkout would silently keep its run state in a local sqlite file."""

    if active_project() is not None:
        return None
    try:
        if find_legacy_checkout() is None:
            return None
    except UnsafeProjectError:
        return None
    if settings.control_db_url.get_secret_value() != DEFAULT_CONTROL_DB_URL:
        return None
    return Check(
        "control db",
        "config",
        "warn",
        "using the local sqlite fallback, not the stack's Postgres",
        hint=(
            "Point HARBORRAG_CONTROL_DB_URL at the stack's Postgres before ingesting, exported "
            f"or as a line in {env_hint('database')}; ingest run/start refuse the fallback in a "
            "checkout."
        ),
        required=False,
    )


def _qdrant_error(settings: RuntimeSettings) -> str | None:
    error = probes.http_ok(settings.qdrant_url.rstrip("/") + "/readyz")
    if error is not None or not settings.qdrant_prefer_grpc:
        return error
    host, _ = probes.host_port(settings.qdrant_url, default_port=_QDRANT_HTTP_PORT)
    grpc_error = probes.tcp_reachable(host, _QDRANT_GRPC_PORT)
    if grpc_error is None:
        return None
    # The runtime has no gRPC-port setting either: the client uses Qdrant's default.
    return (
        f"{grpc_error}; the client prefers Qdrant gRPC (default port {_QDRANT_GRPC_PORT}) "
        "and HTTP alone is not enough"
    )


def _service_checks(settings: RuntimeSettings) -> list[Check]:
    checks = [
        _service_check(
            "qdrant",
            probes.safe_url(settings.qdrant_url),
            _qdrant_error(settings),
            hint_extra=(
                "or set HARBORRAG_QDRANT_PREFER_GRPC=false to use HTTP only"
                if settings.qdrant_prefer_grpc
                else None
            ),
        ),
        _service_check(
            "falkordb",
            f"{settings.falkordb_host}:{settings.falkordb_port}",
            probes.redis_ping(settings.falkordb_host, settings.falkordb_port),
        ),
    ]
    if settings.object_store_provider != "s3":
        # The runtime never touches the S3 endpoint for these providers, so probing it
        # could only report a false failure (or a false pass).
        checks.append(_local_object_store_check(settings))
        return checks
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
    if error is None:
        code = probes.s3_credentials(
            endpoint,
            settings.object_store_region,
            _secret(settings.object_store_access_key_id),
            _secret(settings.object_store_secret_access_key),
            _secret(settings.object_store_session_token),
        )
        if code == probes.UNVERIFIED:
            # Reachable, but nothing could sign a request: never report that as verified.
            checks.append(
                Check(
                    "object store",
                    "services",
                    "warn",
                    f"{probes.safe_url(endpoint)} reachable; "
                    "credentials not verified: botocore not installed",
                    hint="Install botocore to let doctor verify the object-store credentials.",
                    required=False,
                )
            )
            return checks
        if code is not None:
            # Only the S3 error code: never the key, the secret or botocore's message.
            checks.append(
                Check(
                    "object store",
                    "services",
                    "fail",
                    (
                        f"{probes.safe_url(endpoint)} could not list buckets ({code})"
                        if code == "unreachable"
                        else f"{probes.safe_url(endpoint)} rejected the credentials ({code})"
                    ),
                    hint=(
                        "Set HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID/SECRET_ACCESS_KEY "
                        "(checkout: MINIO_ROOT_* in env/.env.database)"
                    ),
                )
            )
            return checks
    checks.append(_service_check("object store", probes.safe_url(endpoint), error))
    return checks


def _local_object_store_check(settings: RuntimeSettings) -> Check:
    provider = settings.object_store_provider
    if provider != "filesystem":
        detail = f"provider {provider!r} is not checked"
        return Check("object store", "services", "skip", detail, required=False)
    root = Path(settings.object_store_root).expanduser()
    error = _writable_directory_error(root)
    if error is not None:
        return Check(
            "object store",
            "services",
            "fail",
            f"filesystem {root}: {error}",
            hint="Set HARBORRAG_OBJECT_STORE_ROOT to a directory this user can write.",
        )
    return Check("object store", "services", "ok", f"filesystem {root}")


def _writable_directory_error(root: Path) -> str | None:
    """Whether ``root`` exists as, or can be created as, a writable directory."""

    candidate = root.absolute()
    while not candidate.exists():
        if candidate.parent == candidate:
            return "no existing parent directory"
        candidate = candidate.parent
    if not candidate.is_dir():
        return f"{candidate} is not a directory"
    if not os.access(candidate, os.W_OK | os.X_OK):
        return f"{candidate} is not writable"
    return None


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def _service_check(
    name: str, target: str, error: str | None, *, hint_extra: str | None = None
) -> Check:
    """``target`` is shown verbatim on success, so callers pass an already-safe string."""

    if error is None:
        return Check(name, "services", "ok", target)
    hint = f"Start the local services: {env_hint('services')}"
    if hint_extra:
        hint = f"{hint} ({hint_extra})"
    return Check(name, "services", "fail", error, hint=hint)


async def _control_plane_checks(
    service_factory: Callable[[], BaseAppService],
    *,
    temporal: bool,
) -> tuple[list[Check], dict[str, object] | None]:
    """Control-plane and Temporal checks, plus the health diagnostics block if available."""

    try:
        service = await asyncio.to_thread(service_factory)
    except Exception as exc:  # noqa: BLE001
        # Never interpolate the raw exception: a composition failure routinely carries
        # HARBORRAG_CONTROL_DB_URL, password included. The logs keep the full detail.
        logger.debug("Control-plane service could not be built", exc_info=True)
        detail = check_error_detail(exc)
        hint = f"Check HARBORRAG_CONTROL_DB_URL in {env_hint('settings')}."
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
        # `--temporal` is an explicit request to check it, so its failure is blocking.
        # Only the unrequested `_temporal_skipped()` case stays non-required.
        if importlib.util.find_spec("temporalio") is None:
            detail = "temporalio is not installed"
            checks.append(Check("temporal", "durable", "fail", detail, hint=_TEMPORAL_CLIENT_HINT))
            return checks, diagnostics
        runtime = await service.runtime_health()
        checks.append(
            Check(
                "temporal",
                "durable",
                "ok" if runtime.ok else "fail",
                runtime.error or "ready",
                hint=_TEMPORAL_HINT,
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
