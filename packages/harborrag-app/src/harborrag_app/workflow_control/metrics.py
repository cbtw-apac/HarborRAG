"""Dashboard metrics summary (ML1/M1): derived from existing rows only.

No dedicated metrics table exists yet — this aggregates the counters already
stored on Project/Source and the per-status job counts already backed by
JobRepositoryPort.count_by_status(), so the endpoint stays honest about what
data is actually available before ML2 wires job creation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_core.domain.job import JobStatus
from harborrag_core.domain.project import Project
from harborrag_core.domain.source_config import SourceConfig

JOB_STATUSES: tuple[JobStatus, ...] = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)


def summarize_metrics(
    projects: Sequence[Project],
    sources: Sequence[SourceConfig],
    jobs_by_status: Mapping[str, int],
) -> dict[str, Any]:
    """Aggregate dashboard counters from already-loaded rows.

    Projects onto the known JOB_STATUSES rather than indexing ``jobs_by_status``
    directly: a legacy row from before a status enum migration must not crash
    the dashboard, it should just not be counted in the breakdown.
    """
    return {
        "projects_total": len(projects),
        "sources_total": len(sources),
        "documents_total": sum(project.stats.documents for project in projects),
        "chunks_total": sum(project.stats.chunks for project in projects),
        "jobs_by_status": {status: jobs_by_status.get(status, 0) for status in JOB_STATUSES},
    }
