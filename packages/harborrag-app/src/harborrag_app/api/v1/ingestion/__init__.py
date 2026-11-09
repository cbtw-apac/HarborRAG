"""Ingestion runs under ``/v1/admin/ingestions`` and the failure inbox under ``/v1/admin/failures``."""

from .failure_routes import router as failures_router
from .routes import router

__all__ = ["failures_router", "router"]
