"""Runner-side job projection.

JobStatus is owned by harborrag_core.domain.job; this module
re-exports it so existing runtime imports keep working. JobState
is the thin persistence/runner view — the full domain aggregate
is core's Job.
"""

from dataclasses import dataclass, field

from harborrag_core.domain.job import JobStatus

__all__ = ["InMemoryJobStore", "JobState", "JobStatus"]


@dataclass(slots=True)
class JobState:
    """Minimal job record the runtime tracks while executing."""

    id: str
    status: JobStatus = "queued"
    error: str | None = None


@dataclass(slots=True)
class InMemoryJobStore:
    """Dict-backed job store for local/mock composition."""

    jobs: dict[str, JobState] = field(default_factory=dict)

    def save(self, job: JobState) -> None:
        """Insert or overwrite the record for job.id."""
        self.jobs[job.id] = job

    def get(self, job_id: str) -> JobState | None:
        """Return the record for job_id, or None if never saved."""
        return self.jobs.get(job_id)
