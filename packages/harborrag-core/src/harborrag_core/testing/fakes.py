from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass, field

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.job import Job
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord


@dataclass(slots=True)
class FakeConnector:
    provider_name: str = "fake"
    documents: list[RawDocument] = field(default_factory=list)

    def discover(self) -> Iterable[SourceRecord]:
        for raw in self.documents:
            yield SourceRecord(id=raw.id, source_type=raw.content_type, locator=raw.source)

    def load(self, record: SourceRecord) -> RawDocument:
        for raw in self.documents:
            if raw.id == record.id:
                return raw
        raise KeyError(record.id)


@dataclass(slots=True)
class FakeParser:
    parser_name: str = "fake"

    def parse(self, raw: RawDocument) -> ParsedDocument:
        text = raw.text()
        return ParsedDocument(
            content=text,
            parser_name=self.parser_name,
            elements=[DocumentElement(id=f"{raw.id}:0", type="paragraph", content=text)],
        )


@dataclass(slots=True)
class FakeJobQueue:
    """In-memory JobQueuePort: FIFO claim, retryable re-queue, cancel."""

    jobs: dict[str, Job] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)

    async def enqueue(self, job: Job) -> Job:
        """Add a job to the tail of the queue."""
        self.jobs[job.id] = job
        self.pending.append(job.id)
        return job

    async def claim_next(self, lease_seconds: int) -> Job | None:
        """Claim the head of the queue; lease_seconds is unused by the fake."""
        while self.pending:
            job = self.jobs[self.pending.pop(0)]
            if job.status != "queued":
                continue
            job.status = "running"
            job.attempts += 1
            return job
        return None

    async def mark_done(self, job_id: str) -> None:
        """Running -> succeeded."""
        self.jobs[job_id].status = "succeeded"

    async def mark_failed(self, job_id: str, error: str, retryable: bool) -> None:
        """Record error; re-queue when retryable, else mark failed."""
        job = self.jobs[job_id]
        job.last_error = error
        if retryable:
            job.status = "queued"
            self.pending.append(job_id)
        else:
            job.status = "failed"

    async def cancel(self, job_id: str) -> None:
        """Any state -> cancelled; never claimable afterwards."""
        self.jobs[job_id].status = "cancelled"


@dataclass(slots=True)
class FakeSecrets:
    """In-memory SecretsPort; refs are sequential and reveal nothing."""

    values: dict[str, str] = field(default_factory=dict)
    counter: int = 0

    async def put(self, value: str) -> str:
        """Store the value under a fresh opaque ref."""
        self.counter += 1
        ref = f"secret://fake/{self.counter}"
        self.values[ref] = value
        return ref

    async def resolve(self, ref: str) -> str:
        """Return the stored value; KeyError for unknown/deleted refs."""
        return self.values[ref]

    async def delete(self, ref: str) -> None:
        """Forget the value behind the ref."""
        self.values.pop(ref, None)


@dataclass(slots=True)
class FakeEventBus:
    """Recording EventBusPort: subscribe replays already-published events.

    Deterministic by design — no live fan-out, so tests never hang waiting
    on a stream. The real bus (M2) streams indefinitely.
    """

    events: list[HarborEvent] = field(default_factory=list)

    async def publish(self, event: HarborEvent) -> None:
        """Record the event."""
        self.events.append(event)

    def subscribe(self, name_prefix: str) -> AsyncGenerator[HarborEvent, None]:
        """Yield recorded events whose name starts with name_prefix."""

        async def _replay() -> AsyncGenerator[HarborEvent, None]:
            for event in list(self.events):
                if event.name.startswith(name_prefix):
                    yield event

        return _replay()
