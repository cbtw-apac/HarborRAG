"""FastAPI application factory for the HarborRAG Control Plane API (ST2).

`uvicorn harborrag_app.api.app:create_fastapi_app --factory` is the deploy
entrypoint (Dockerfile.api CMD). The factory wires settings, trace middleware,
CORS, the error envelope, auth verifier, and all routers under /api/v1; the
lifespan builds the app service through runtime composition.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from harborrag_app.api.auth.dependencies import build_token_verifier
from harborrag_app.api.errors import register_error_handlers
from harborrag_app.api.middleware import TraceIdMiddleware
from harborrag_app.api.routes import all_routers
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.selection import select_app_service
from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_core.observability.process_logging import configure_logging

API_PREFIX = "/api/v1"

logger = logging.getLogger("harborrag.app.api.app")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Compose the app service on startup (ST8 selection rule).

    Runs in a worker thread: production composition executes Alembic
    migrations and a DB probe, which drive their own event loops.
    """
    service, mode = await asyncio.to_thread(select_app_service)
    app.state.app_service = service
    app.state.composition_mode = mode
    # The dev/prod selection is driven by HARBORRAG_ENV plus the presence of
    # HARBORRAG_CONTROL_DB_URL. "development" stubs the control-plane database
    # while still driving the real Temporal runtime, so an operator who expects
    # production composition must be able to see which branch was taken.
    logger.info("Application service composed in %s mode", mode)
    try:
        yield
    finally:
        logger.info("Closing the application service")
        close = getattr(service, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


def create_fastapi_app(settings: ApiSettings | None = None) -> FastAPI:
    """Build the Control Plane API app from (env-derived) settings."""
    settings = settings or ApiSettings()
    # uvicorn configures the root logger, not the "harborrag" namespace; this
    # attaches the namespace handler so runtime and route logs are emitted.
    configure_logging()
    app = FastAPI(
        title="HarborRAG Control Plane API",
        version="1.0.0-draft",
        openapi_url=f"{API_PREFIX}/openapi.json" if settings.docs_enabled else None,
        docs_url=f"{API_PREFIX}/docs" if settings.docs_enabled else None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.token_verifier = build_token_verifier(settings)
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
    for router in all_routers():
        app.include_router(router, prefix=API_PREFIX)
    return app
