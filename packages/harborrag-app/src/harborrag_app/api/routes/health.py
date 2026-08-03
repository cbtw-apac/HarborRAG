"""Dependency-free process liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    """Liveness: the process is up; never blocks on dependencies."""
    return {"status": "ok", "version": request.app.version}
