"""Top-level router composition for the HTTP application."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI

from harborrag_app.api.deadline import enforce_deadline
from harborrag_app.api.probes import router as probes_router
from harborrag_app.api.v1.admin.connection_routes import routers as connection_admin_routers
from harborrag_app.api.v1.admin.settings_routes import router as settings_router
from harborrag_app.api.v1.chat import router as chat_router
from harborrag_app.api.v1.ingestion import failures_router
from harborrag_app.api.v1.ingestion import router as ingestion_router
from harborrag_app.api.v1.me import router as me_router

PUBLIC_PREFIX = "/v1"


def register_routes(app: FastAPI) -> None:
    """Mount the probes at the root and the public API under ``/v1``."""

    app.include_router(probes_router)
    public = APIRouter(
        prefix=PUBLIC_PREFIX,
        dependencies=[Depends(enforce_deadline, scope="function")],
    )
    for router in (
        me_router,
        chat_router,
        *connection_admin_routers,
        settings_router,
        ingestion_router,
        failures_router,
    ):
        public.include_router(router)
    app.include_router(public)
