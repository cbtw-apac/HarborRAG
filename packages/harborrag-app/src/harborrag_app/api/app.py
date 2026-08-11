"""FastAPI application factory for the HarborRAG Control Plane API (ST2).

`uvicorn harborrag_app.api.app:create_fastapi_app --factory` is the deploy
entrypoint (Dockerfile.api CMD). The factory wires settings, observability,
trace middleware, CORS, the error envelope, auth verifier, and versioned public
routers; the lifespan builds the app service through runtime composition.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.routing import compile_path

from harborrag_app.api.auth.dependencies import build_token_verifier
from harborrag_app.api.capacity import build_api_capacity_limiter
from harborrag_app.api.errors import register_error_handlers
from harborrag_app.api.metrics import ApiMetrics, ApiMetricsMiddleware
from harborrag_app.api.middleware import RequestBodyLimitMiddleware, TraceIdMiddleware
from harborrag_app.api.router import OPERATIONAL_PREFIX, register_routes
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.composition.selection import select_app_service
from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_core.observability.process_logging import configure_logging

logger = logging.getLogger("harborrag.app.api.app")
_SUBMISSION_RECOVERY_INTERVAL_SECONDS = 5.0
_INGESTION_PROGRESS_INTERVAL_SECONDS = 2.0


def _redirect_root(request: Request) -> RedirectResponse:
    """Send humans to docs when enabled, otherwise to the health endpoint."""
    settings: ApiSettings = request.app.state.settings
    destination = "docs" if settings.docs_enabled else "health"
    return RedirectResponse(url=f"{OPERATIONAL_PREFIX}/{destination}")


def _redirect_docs() -> RedirectResponse:
    """Preserve the familiar Swagger path without duplicating the API route."""
    return RedirectResponse(url=f"{OPERATIONAL_PREFIX}/docs")


async def _recover_pending_submissions(app: FastAPI) -> None:
    """Continuously drain the durable workflow-submission outbox."""

    while True:
        try:
            recovered = await app.state.app_service.recover_pending_submissions()
            if recovered:
                logger.info("Recovered %d pending ingestion submissions", recovered)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pending ingestion submission recovery pass failed")
        await asyncio.sleep(_SUBMISSION_RECOVERY_INTERVAL_SECONDS)


async def _sync_ingestion_progress(app: FastAPI) -> None:
    """Continuously fan active ingestion tasks' progress out via the event bus."""

    while True:
        try:
            await app.state.app_service.sync_ingestion_progress()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ingestion progress sync tick failed")
        await asyncio.sleep(_INGESTION_PROGRESS_INTERVAL_SECONDS)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Compose the app service on startup (ST8 selection rule).

    Runs in a worker thread: production composition executes Alembic
    migrations and a DB probe, which drive their own event loops.
    """
    service, mode = await asyncio.to_thread(select_app_service)
    app.state.app_service = service
    app.state.composition_mode = mode
    logger.info("Application service composed in %s mode", mode)
    recovery_task = asyncio.create_task(_recover_pending_submissions(app))
    progress_task = asyncio.create_task(_sync_ingestion_progress(app))
    try:
        yield
    finally:
        recovery_task.cancel()
        progress_task.cancel()
        await asyncio.gather(recovery_task, progress_task, return_exceptions=True)
        logger.info("Closing the application service")
        try:
            close = getattr(service, "aclose", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        finally:
            await app.state.api_capacity_limiter.aclose()


def create_fastapi_app(settings: ApiSettings | None = None) -> FastAPI:
    """Build the Control Plane API app from (env-derived) settings."""
    settings = settings or ApiSettings()
    # uvicorn configures the root logger, not the "harborrag" namespace; this
    # attaches the namespace handler so runtime and route logs are emitted.
    configure_logging()
    app = FastAPI(
        title="HarborRAG Control Plane API",
        version="0.1.0",
        openapi_url=(f"{OPERATIONAL_PREFIX}/openapi.json" if settings.docs_enabled else None),
        docs_url=f"{OPERATIONAL_PREFIX}/docs" if settings.docs_enabled else None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.token_verifier = build_token_verifier(settings)
    redis_url = (
        settings.api_capacity_redis_url.get_secret_value()
        if settings.api_capacity_redis_url is not None
        else None
    )
    app.state.api_capacity_limiter = build_api_capacity_limiter(
        redis_url=redis_url,
        requests_per_minute=settings.api_requests_per_minute,
        max_inflight=settings.api_max_inflight_per_principal,
        lease_seconds=settings.api_request_timeout_seconds + 5,
    )
    app.state.api_metrics = ApiMetrics(version=app.version)
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
        )
    register_error_handlers(app)
    app.add_api_route("/", _redirect_root, methods=["GET"], include_in_schema=False)
    if settings.docs_enabled:
        app.add_api_route(
            "/docs",
            _redirect_docs,
            methods=["GET"],
            include_in_schema=False,
        )
    register_routes(app)
    app.state.api_metric_routes = tuple(
        (path, compile_path(path)[0]) for path in app.openapi()["paths"]
    )
    return app
