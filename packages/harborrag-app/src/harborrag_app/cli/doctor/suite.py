"""Assemble the layered readiness checks: project, config, environment, services."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path

from pydantic import SecretStr

from harborrag_app.api.readiness import Check as ReadinessCheck
from harborrag_app.api.readiness import ReadinessChecks, build_readiness_checks
from harborrag_app.cli.invoke import close
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
from .checks import SERVICES_GROUP, Check, DoctorReport
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

_READINESS_TIMEOUT_SECONDS = 6.0

_PROJECT_HINT = "Run harborrag inside a HarborRAG repository checkout, or pass --project DIR."
_CONNECTORS_HINT = (
    "Fix or remove the connector named above in config/sources/connectors.yaml before the first "
    "start; after it, change connections through /v1/admin/projects/.../connections."
)
_TEMPORAL_HINT = (
    "Durable mode needs the Temporal stack (scripts/deployment/dev.sh up in a checkout)."
)


async def run_doctor(*, service_factory: Callable[[], BaseAppService]) -> DoctorReport:
    """Run every check and list the tenants that hold data.

    Args:
        service_factory: Builds the application service the control-plane, Temporal and
            tenant checks use; a failure to build it is a failed check, not an error.

    Returns:
        The checks in order: project and configuration, control plane, readiness, object
        store.
    """
    checks: list[Check] = [_project_check(), packages_check()]
    settings = _settings(checks)
    if settings is None:
        return DoctorReport(tuple(checks))
    checks.extend(_configuration_checks(settings))
    control_db = _control_db_check(settings)
    if control_db is not None:
        checks.append(control_db)
    service, control_plane = await _control_plane_check(service_factory)
    checks.append(control_plane)
    try:
        checks.extend(await _readiness_checks(settings, service))
        checks.extend(await asyncio.to_thread(_object_store_checks, settings))
        tenants, tenant_check = await _tenants(service)
        if tenant_check is not None:
            checks.append(tenant_check)
        return DoctorReport(tuple(checks), diagnostics=_diagnostics(service), tenants=tenants)
    finally:
        if service is not None:
            await close(service)


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
            hint=_PROJECT_HINT,
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
        checks.append(Check("connectors catalog", "config", "fail", str(exc), hint=_PROJECT_HINT))
    try:
        load_parser_catalog(settings.parser_config_path)
        checks.append(Check("parsers catalog", "config", "ok", str(settings.parser_config_path)))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("parsers catalog", "config", "fail", str(exc), hint=_PROJECT_HINT))
    checks.append(_chunking_check(settings))
    checks.append(models_check(settings.model_config_path))
    if catalog is not None:
        checks.extend(source_path_checks(catalog))
        credentials = credentials_check(catalog)
        if credentials is not None:
            checks.append(credentials)
    return checks


def _chunking_check(settings: RuntimeSettings) -> Check:
    """Validate chunking.yaml and report when code would split without its grammars."""

    from harborrag_runtime.config.chunking import chunking_settings_from
    from harborrag_runtime.ingestion.chunking_profile import code_grammar_problems

    try:
        chunking = chunking_settings_from(settings)
    except Exception as exc:  # noqa: BLE001 - reported as a check, never raised
        return Check("chunking policy", "config", "fail", str(exc), hint=_PROJECT_HINT)
    detail = str(chunking.source_path) if chunking.source_path else "built-in defaults"
    problems = code_grammar_problems(chunking.refiner)
    if not problems:
        return Check("chunking policy", "config", "ok", detail)
    return Check(
        "chunking policy",
        "config",
        "fail" if chunking.refiner.require_code_grammars else "warn",
        f"{detail}; code splits at separators ({len(problems)} grammar packages missing)",
        hint='Install syntax-aware code splitting: pip install "harborrag-adapters[chunking-code]"',
    )


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


def _object_store_checks(settings: RuntimeSettings) -> list[Check]:
    checks: list[Check] = []
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


async def _control_plane_check(
    service_factory: Callable[[], BaseAppService],
) -> tuple[BaseAppService | None, Check]:
    """Build the service and report its control-plane health."""

    try:
        service = await asyncio.to_thread(service_factory)
    except Exception as exc:  # noqa: BLE001
        # Never interpolate the raw exception: a composition failure routinely carries
        # HARBORRAG_CONTROL_DB_URL, password included. The logs keep the full detail.
        logger.debug("Control-plane service could not be built", exc_info=True)
        hint = f"Check HARBORRAG_CONTROL_DB_URL in {env_hint('settings')}."
        detail = check_error_detail(exc)
        return None, Check("control plane", SERVICES_GROUP, "fail", detail, hint=hint)
    try:
        health = service.health()
    except BaseException:
        await close(service)
        raise
    detail = health.error or "ready"
    return service, Check("control plane", SERVICES_GROUP, "ok" if health.ok else "fail", detail)


def _diagnostics(service: BaseAppService | None) -> dict[str, object] | None:
    """The health diagnostics block of a built service."""

    if service is None:
        return None
    raw = service.health().data.get("diagnostics")
    return dict(raw) if isinstance(raw, dict) else None


async def _readiness_checks(
    settings: RuntimeSettings, service: BaseAppService | None
) -> list[Check]:
    """Run the checks ``/readyz`` runs; Redis is the runtime's only, as there is no API."""

    checks = build_readiness_checks(
        settings, capacity_redis_url=None, temporal=_temporal_probe(service)
    )
    report = await ReadinessChecks(checks, timeout_seconds=_READINESS_TIMEOUT_SECONDS).report()
    return [
        _service_check(
            name,
            "ready",
            None if result == "ok" else "did not answer the readiness probe",
            hint_extra=_TEMPORAL_HINT if name == "temporal" else None,
        )
        for name, result in report.items()
    ]


def _temporal_probe(service: BaseAppService | None) -> ReadinessCheck:
    """Ask the workflow service for its health; fail when the service could not be built."""

    async def probe() -> bool:
        return service is not None and (await service.runtime_health()).ok

    return probe


async def _tenants(service: BaseAppService | None) -> tuple[tuple[str, ...], Check | None]:
    """The tenants holding data, or a failed check when the control plane cannot be read."""

    if service is None:
        return (), None
    try:
        return tuple(await service.tenants_with_data()), None
    except Exception as exc:  # noqa: BLE001 - reported as a check, never raised
        logger.debug("Tenants could not be listed", exc_info=True)
        return (), Check("tenants", SERVICES_GROUP, "fail", check_error_detail(exc))
