"""Dashboard metrics summary (ML1/M1): derived from existing rows only.

No dedicated metrics table exists yet — this aggregates the counters already
stored on Project/Source and the job rows already backed by JobRepositoryPort,
so the endpoint stays honest about what data is actually available before
ML2 wires job creation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from harborrag_core.domain.job import Job, JobStatus
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
    jobs: Sequence[Job],
) -> dict[str, Any]:
    """Aggregate dashboard counters from already-loaded rows."""
    jobs_by_status = dict.fromkeys(JOB_STATUSES, 0)
    for job in jobs:
        jobs_by_status[job.status] += 1
    return {
        "projects_total": len(projects),
        "sources_total": len(sources),
        "documents_total": sum(project.stats.documents for project in projects),
        "chunks_total": sum(project.stats.chunks for project in projects),
        "jobs_by_status": jobs_by_status,
    }
