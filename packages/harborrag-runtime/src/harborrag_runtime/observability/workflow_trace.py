"""One trace per Temporal workflow run, shared by every activity that run executes.

The trace ID is derived from the workflow and run IDs, so activities that run on
different workers, attempts or days still land in the same trace. Child workflows get
their own trace, which keeps a source run of thousands of documents from becoming one
unreadable trace.

Typical usage example:

    with workflow_trace(info.workflow_id, info.workflow_run_id):
        run_activity()
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

_TRACE_ID_BYTES = 16
_SPAN_ID_BYTES = 8


@contextmanager
def workflow_trace(workflow_id: str, run_id: str) -> Iterator[None]:
    """Make spans opened in the block children of the workflow run's trace.

    Args:
        workflow_id: The Temporal workflow ID.
        run_id: The Temporal run ID of that workflow.

    Yields:
        Nothing. The workflow run's trace is current until the block ends.
    """
    token = context.attach(trace.set_span_in_context(_workflow_parent(workflow_id, run_id)))
    try:
        yield
    finally:
        context.detach(token)


def _workflow_parent(workflow_id: str, run_id: str) -> NonRecordingSpan:
    """Return the remote parent span every activity of one workflow run shares."""
    digest = hashlib.sha256(f"{workflow_id}/{run_id}".encode()).digest()
    trace_id = int.from_bytes(digest[:_TRACE_ID_BYTES], "big") or 1
    span_id = int.from_bytes(digest[_TRACE_ID_BYTES : _TRACE_ID_BYTES + _SPAN_ID_BYTES], "big") or 1
    return NonRecordingSpan(
        SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    )
