"""Top-level router composition for the HTTP application."""

from __future__ import annotations

from fastapi import FastAPI

from harborrag_app.api.probes import router as probes_router
from harborrag_app.api.v1.admin.connection_routes import routers as connection_admin_routers
from harborrag_app.api.v1.chat import router as chat_router
from harborrag_app.api.v1.ingestion import router as ingestion_router

PUBLIC_PREFIX = "/v1"


def register_routes(app: FastAPI) -> None:
    """Mount the probes at the root and the public API under ``/v1``."""

    app.include_router(probes_router)
    # TODO: remove the legacy /v1/ingestions routes when /v1/admin/ingestions lands.
    for router in (ingestion_router, chat_router, *connection_admin_routers):
        app.include_router(router, prefix=PUBLIC_PREFIX)
