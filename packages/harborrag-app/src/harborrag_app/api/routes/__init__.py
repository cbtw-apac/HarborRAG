"""Router registry for the Control Plane API (ST2).

Milestones append routers here (M1: projects/sources/activity/settings/metrics,
M2: jobs + streams, ...); the factory mounts everything under /api/v1.
"""

from __future__ import annotations

from fastapi import APIRouter

from harborrag_app.api.routes import diagnostics, health


def all_routers() -> list[APIRouter]:
    """Every router the factory mounts, in registration order."""
    return [health.router, diagnostics.router]
