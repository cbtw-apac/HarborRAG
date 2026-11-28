"""FastAPI application factory for the HarborRAG Control Plane API (ST2).

`uvicorn harborrag_app.api.app:create_fastapi_app --factory` is the deploy
entrypoint (Dockerfile.api CMD). The factory wires settings, observability,
trace middleware, CORS, the error envelope, auth verifier, and versioned public
routers. The lifespan builds the app service through runtime composition and runs
the background loops, which stop at shutdown and never block startup.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.routing import compile_path

from harborrag_app import __version__
from harborrag_app.api.auth.dependencies import build_token_verifier
from harborrag_app.api.capacity import build_api_capacity_limiter
from harborrag_app.api.errors import register_error_handlers
from harborrag_app.api.metrics import ApiMetrics, ApiMetricsMiddleware
from harborrag_app.api.middleware import TRACE_HEADER, RequestBodyLimitMiddleware, TraceIdMiddleware
from harborrag_app.api.openapi import install_openapi
from harborrag_app.api.ops import OpsPortMiddleware
from harborrag_app.api.rate_limit import build_rate_limiter
from harborrag_app.api.router import register_routes
from harborrag_app.api.settings import ApiSettings
from harborrag_app.operations.local import LocalOperations
from harborrag_core.contracts.errors import HarborConfigurationError, HarborUnavailableError
from harborrag_core.domain.identity import DEFAULT_TENANT_ID
from harborrag_core.observability.log_events import FailureKind, log_event, log_failure
from harborrag_core.observability.process_logging import configure_logging
from harborrag_runtime.application.composition.selection import select_app_service
from harborrag_runtime.errors import MissingOptionalDependencyError
from harborrag_runtime.observability.export import TelemetryService, configure_telemetry_export

if TYPE_CHECKING:
    from harborrag_core.ports.source_freshness import SourceFreshnessReader
    from harborrag_runtime.application.composition.service import AppService

logger = logging.getLogger("harborrag.app.api.app")
_SUBMISSION_RECOVERY_INTERVAL_SECONDS = 5.0
_INGESTION_PROGRESS_INTERVAL_SECONDS = 2.0
_CONTROL_PLANE_EFFECT_RECOVERY_INTERVAL_SECONDS = 30.0
_CONNECTION_REMOVAL_RECOVERY_INTERVAL_SECONDS = 60.0
_SCHEDULE_SYNC_RETRY_INTERVAL_SECONDS = 30.0
_SOURCE_FRESHNESS_REFRESH_INTERVAL_SECONDS = 60.0
_SOURCE_FRESHNESS_REFRESH_TIMEOUT_SECONDS = 15.0
_SOURCE_METRICS_LIMIT = 1000
"""The most sources the freshness gauges export, which bounds their series."""
_SCHEDULE_SLOT_RESTART_INTERVAL_SECONDS = 30.0


def _redirect_root(request: Request) -> RedirectResponse:
    """Send humans to docs when enabled, otherwise to the liveness probe."""
    settings: ApiSettings = request.app.state.settings
    return RedirectResponse(url="/docs" if settings.docs_enabled else "/healthz")


async def _recover_pending_submissions(app: FastAPI) -> None:
    """Continuously drain the durable workflow-submission outbox."""
    while True:
        try:
            recovered = await app.state.app_service.recover_pending_submissions()
            if recovered:
                log_event(logger, "api.submissions_recovered", recovered_count=recovered)
        except Exception as exc:  # noqa: BLE001 - background loop boundary; the next pass retries
            log_failure(logger, "api.submission_recovery_failed", exc)
        await asyncio.sleep(_SUBMISSION_RECOVERY_INTERVAL_SECONDS)


async def _sync_ingestion_progress(app: FastAPI) -> None:
    """Continuously fan active ingestion tasks' progress out via the event bus."""
    while True:
        try:
            await app.state.app_service.sync_ingestion_progress()
        except Exception as exc:  # noqa: BLE001 - background loop boundary; the next tick retries
            log_failure(logger, "api.ingestion_progress_sync_failed", exc)
        await asyncio.sleep(_INGESTION_PROGRESS_INTERVAL_SECONDS)


async def _recover_pending_control_plane_effects(app: FastAPI) -> None:
    """Continuously drain the durable secret-retirement/audit-logging outbox."""
    while True:
        try:
            recovered = await app.state.app_service.recover_pending_control_plane_effects()
            if recovered:
                log_event(logger, "api.control_plane_effects_recovered", recovered_count=recovered)
        except Exception as exc:  # noqa: BLE001 - background loop boundary; the next pass retries
            log_failure(logger, "api.control_plane_effect_recovery_failed", exc)
        await asyncio.sleep(_CONTROL_PLANE_EFFECT_RECOVERY_INTERVAL_SECONDS)


async def _import_connectors_once(service: object) -> None:
    """Carry an existing ``config/sources/connectors.yaml`` into the database on the first start."""
    importer = getattr(service, "import_connectors_once", None)
    if importer is None:
        return
    try:
        imported = await importer()
    except Exception as exc:  # noqa: BLE001 - startup continues; connections can be added later
        log_failure(logger, "api.connectors_import_failed", exc)
        log_event(
            logger,
            "api.connectors_import_skipped",
            level=logging.WARNING,
            hint="Add the connections in config/sources/connectors.yaml through the API.",
        )
        return
    if imported:
        log_event(logger, "api.connectors_imported", imported_count=imported)


async def _seed_settings_once(service: object) -> None:
    """Seed ``config/settings.yaml`` into an empty settings log, else report drift."""
    seeder = getattr(service, "seed_settings_once", None)
    if seeder is None:
        return
    try:
        await seeder()
    except Exception as exc:  # noqa: BLE001 - startup continues; settings can be set later
        log_failure(logger, "api.settings_seed_failed", exc)
        log_event(
            logger,
            "api.settings_seed_skipped",
            level=logging.WARNING,
            hint="Set the settings from the settings file through the settings API.",
        )


async def _resume_connection_removals(app: FastAPI) -> None:
    """Finish connection and project removals a crash or failure left unfinished."""
    while True:
        try:
            resumed = await app.state.app_service.resume_connection_removals()
            if resumed:
                log_event(logger, "api.connection_removals_resumed", resumed_count=resumed)
        except Exception as exc:  # noqa: BLE001 - background loop boundary; the next pass retries
            log_failure(logger, "api.connection_removal_recovery_failed", exc)
        await asyncio.sleep(_CONNECTION_REMOVAL_RECOVERY_INTERVAL_SECONDS)


async def _sync_declared_schedules(app: FastAPI) -> None:
    """Reconcile declared schedules once, retrying while the engine is unreachable.

    Runs in the background so a Temporal outage never blocks API startup.
    Invalid configuration (``ValueError``) and a missing Temporal SDK
    (``ImportError``) cannot heal without a redeploy, so they stop the loop.
    """
    while True:
        try:
            await app.state.app_service.sync_declared_schedules()
            return
        except (ValueError, ImportError) as exc:
            log_event(
                logger,
                "api.declared_schedule_sync_failed",
                level=logging.ERROR,
                hint="Fix the schedule configuration or install the Temporal SDK, then restart.",
                error=exc,
                retryable=False,
            )
            return
        except Exception as exc:  # noqa: BLE001 - background loop boundary; the loop retries
            log_failure(
                logger,
                "api.declared_schedule_sync_failed",
                exc,
                retryable=True,
                retry_in_seconds=_SCHEDULE_SYNC_RETRY_INTERVAL_SECONDS,
            )
        await asyncio.sleep(_SCHEDULE_SYNC_RETRY_INTERVAL_SECONDS)


async def _refresh_source_freshness(app: FastAPI) -> None:
    """Refresh the source freshness gauges at once, then every minute until cancelled."""
    while True:
        await _refresh_source_freshness_once(app.state.app_service, app.state.api_metrics)
        await asyncio.sleep(_SOURCE_FRESHNESS_REFRESH_INTERVAL_SECONDS)


async def _refresh_source_freshness_once(
    reader: SourceFreshnessReader, metrics: ApiMetrics
) -> None:
    """Swap in a fresh snapshot of every source's freshness, or keep the last one.

    A failed or timed-out read is logged and leaves the last snapshot in place, so the
    refresh time stops advancing until a read succeeds.
    """
    try:
        async with asyncio.timeout(_SOURCE_FRESHNESS_REFRESH_TIMEOUT_SECONDS):
            report = await reader.source_freshness(limit=_SOURCE_METRICS_LIMIT)
    except HarborUnavailableError as exc:
        log_failure(
            logger, "observability.source_metrics_refresh_failed", exc, FailureKind.EXPECTED
        )
        return
    except TimeoutError as exc:
        log_failure(
            logger,
            "observability.source_metrics_refresh_failed",
            exc,
            FailureKind.EXPECTED,
            error_code="timeout",
        )
        return
    except Exception as exc:  # noqa: BLE001 - background loop boundary; the next refresh retries
        log_failure(logger, "observability.source_metrics_refresh_failed", exc)
        return
    if report.truncated:
        log_event(
            logger,
            "observability.source_metrics_truncated",
            level=logging.WARNING,
            source_count=report.source_count,
            exported_source_count=len(report.sources),
        )
    metrics.record_source_freshness(report.sources, refreshed_at=time.time())


async def _run_schedule_slots(app: FastAPI) -> None:
    """Serve the slots of ``/v1/admin/schedules``, restarting the worker after a failure.

    The worker runs until the lifespan cancels it; a normal return ends the loop. A
    missing Temporal SDK or extra cannot heal without a redeploy, so it stops the
    loop too.
    """
    while True:
        try:
            await app.state.app_service.run_schedule_slots()
            return
        except asyncio.CancelledError:
            raise
        except (ImportError, MissingOptionalDependencyError) as exc:
            log_event(
                logger,
                "api.schedule_slot_worker_unavailable",
                level=logging.ERROR,
                hint="Install the Temporal extra in the API image, then restart the API.",
                error=exc,
                retryable=False,
            )
            return
        except Exception as exc:  # noqa: BLE001 - background loop boundary; the loop restarts it
            log_failure(
                logger,
                "api.schedule_slot_worker_failed",
                exc,
                retryable=True,
                retry_in_seconds=_SCHEDULE_SLOT_RESTART_INTERVAL_SECONDS,
            )
        await asyncio.sleep(_SCHEDULE_SLOT_RESTART_INTERVAL_SECONDS)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Compose the app service on startup (ST8 selection rule).

    Runs in a worker thread: production composition executes Alembic
    migrations and a DB probe, which drive their own event loops.

    selection composes an AppService; tests pass a double.
    """
    service, mode = await asyncio.to_thread(select_app_service)
    app.state.app_service = service
    app.state.operations = LocalOperations(cast("AppService", service))
    app.state.composition_mode = mode
    app.state.default_tenant_id = getattr(service, "default_tenant_id", DEFAULT_TENANT_ID)
    log_event(logger, "api.app_service_composed", mode=mode)
    if hasattr(service, "start_config_provider"):
        await service.start_config_provider()
    await _import_connectors_once(service)
    await _seed_settings_once(service)
    recovery_task = asyncio.create_task(_recover_pending_submissions(app))
    progress_task = asyncio.create_task(_sync_ingestion_progress(app))
    control_plane_effect_recovery_task = asyncio.create_task(
        _recover_pending_control_plane_effects(app)
    )
    background_tasks = [recovery_task, progress_task, control_plane_effect_recovery_task]
    if hasattr(service, "sync_declared_schedules"):
        background_tasks.append(asyncio.create_task(_sync_declared_schedules(app)))
    if hasattr(service, "run_schedule_slots"):
        background_tasks.append(asyncio.create_task(_run_schedule_slots(app)))
    if hasattr(service, "resume_connection_removals"):
        background_tasks.append(asyncio.create_task(_resume_connection_removals(app)))
    if hasattr(service, "source_freshness"):
        background_tasks.append(asyncio.create_task(_refresh_source_freshness(app)))
    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        log_event(logger, "api.app_service_closing")
        try:
            close = getattr(service, "aclose", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        finally:
            await app.state.api_capacity_limiter.aclose()
            await app.state.rate_limiter.aclose()


def create_fastapi_app(settings: ApiSettings | None = None) -> FastAPI:
    """Build the Control Plane API app from (env-derived) settings.

    uvicorn configures the root logger, not the "harborrag" namespace; this attaches the namespace
    handler so runtime and route logs are emitted.

    A lease spans the whole response, including streamed bodies, so it must outlive the longer of
    the two server-owned deadlines.
    """
    settings = settings or ApiSettings()
    configure_logging()
    configure_telemetry_export(TelemetryService.API)
    app = FastAPI(
        title="HarborRAG Control Plane API",
        version=__version__,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        lifespan=_lifespan,
        generate_unique_id_function=lambda route: route.name,
    )
    app.state.settings = settings
    app.state.default_tenant_id = DEFAULT_TENANT_ID
    app.state.token_verifier = build_token_verifier(settings)
    redis_url = (
        settings.api_capacity_redis_url.get_secret_value()
        if settings.api_capacity_redis_url is not None
        else None
    )
    app.state.api_capacity_limiter = build_api_capacity_limiter(
        redis_url=redis_url,
        limits=settings.default_capacity_limits(),
        tenant_overrides=settings.tenant_capacity_limits(),
        lease_seconds=(
            max(settings.api_request_timeout_seconds, settings.api_stream_timeout_seconds) + 5
        ),
    )
    app.state.rate_limiter = build_rate_limiter(redis_url)
    app.state.api_metrics = ApiMetrics(version=app.version)
    app.add_middleware(OpsPortMiddleware, settings=settings, metrics=app.state.api_metrics)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=settings.max_request_body_bytes,
    )
    app.add_middleware(ApiMetricsMiddleware, metrics=app.state.api_metrics)
    app.add_middleware(TraceIdMiddleware)
    if settings.cors_origins:
        if "*" in settings.cors_origins:
            raise HarborConfigurationError(
                "wildcard cors_origins is not allowed with credentialed CORS; list explicit origins"
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[
                TRACE_HEADER,
                "Idempotency-Replayed",
                "Retry-After",
                "ETag",
                "Location",
            ],
        )
    register_error_handlers(app)
    app.add_api_route("/", _redirect_root, methods=["GET"], include_in_schema=False)
    register_routes(app)
    install_openapi(app)
    app.state.api_metric_routes = tuple(
        (path, compile_path(path)[0]) for path in app.openapi()["paths"]
    )
    return app
