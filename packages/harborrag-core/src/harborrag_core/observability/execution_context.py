"""Temporal identity of the activity running in the current context.

Activities bind it once at their boundary; log records, spans and child
processes read it from here instead of threading IDs through every call.
``asyncio.to_thread`` copies context variables, so parser threads see it too.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TemporalExecutionContext:
    namespace: str
    workflow_id: str
    run_id: str
    activity_id: str
    attempt: int

    def temporal_attributes(self) -> dict[str, str | int]:
        return {
            "temporal.namespace": self.namespace,
            "temporal.workflow_id": self.workflow_id,
            "temporal.run_id": self.run_id,
            "temporal.activity_id": self.activity_id,
            "temporal.attempt": self.attempt,
        }

    def span_attributes(self) -> dict[str, str | int]:
        # The document workflow ID doubles as the execution ID Tempo searches by.
        return {"harborrag.document_execution_id": self.workflow_id, **self.temporal_attributes()}


_current: ContextVar[TemporalExecutionContext | None] = ContextVar(
    "harborrag_temporal_execution", default=None
)


def current_execution() -> TemporalExecutionContext | None:
    return _current.get()


@contextmanager
def bound_execution(context: TemporalExecutionContext | None) -> Iterator[None]:
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)
