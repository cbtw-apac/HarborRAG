"""Router registry for the Control Plane API (ST2).

Milestones append routers here (M1: projects/sources/activity/settings/metrics,
M2: jobs done, streams still pending, ...); the factory mounts everything
under /api/v1.
"""

from __future__ import annotations

from fastapi import APIRouter

from harborrag_app.api.routes import (
    activity,
    diagnostics,
    health,
    ingestions,
    jobs,
    metrics,
    projects,
    settings,
    sources,
)


def all_routers() -> list[APIRouter]:
    """Every router the factory mounts, in registration order."""
    return [
        health.router,
        diagnostics.router,
        projects.router,
        sources.router,
        activity.router,
        settings.router,
        metrics.router,
        ingestions.router,
        jobs.router,
    ]
