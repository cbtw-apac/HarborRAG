"""Top-level router composition for the HTTP application."""

from __future__ import annotations

from fastapi import FastAPI

from harborrag_app.api.routes import health, metrics
from harborrag_app.api.v1.admin import router as admin_router
from harborrag_app.api.v1.agent import router as agent_router
from harborrag_app.api.v1.chat import router as chat_router
from harborrag_app.api.v1.ingestion import router as ingestion_router
from harborrag_app.api.v1.retrieval import router as retrieval_router

OPERATIONAL_PREFIX = "/api/v1"
PUBLIC_PREFIX = "/v1"


def register_routes(app: FastAPI) -> None:
    """Mount process routes and stable public resource routes."""

    app.include_router(health.router, prefix=OPERATIONAL_PREFIX)
    app.include_router(metrics.router, prefix=OPERATIONAL_PREFIX)
    for router in (ingestion_router, retrieval_router, chat_router, agent_router, admin_router):
        app.include_router(router, prefix=PUBLIC_PREFIX)
