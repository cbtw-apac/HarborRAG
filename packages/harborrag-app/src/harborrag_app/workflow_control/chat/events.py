"""The transport-neutral ``{"kind": ...}`` events a chat stream yields.

Kept apart from ``service.py`` so that module stays about the turn's control
flow, and apart from ``presenters.py`` so projecting one result is not
confused with describing one event.
"""

from __future__ import annotations

import logging

from harborrag_app.workflow_control.errors import failure_response
from harborrag_app.workflow_control.memory.identity import MemoryIdentity

from .options import ChatExecutionOptions
from .preparation import PreparedTurn
from .presenters import citation_data, cited_results

logger = logging.getLogger("harborrag.app.workflow_control.chat")


def error_event(exc: Exception, message: str) -> dict[str, object]:
    """Log the private detail and describe the failure to the transport.

    ``error`` stays what it has always been -- the reviewed public message,
    which for most exceptions is the class name. ``error_type`` is always the
    class name, which is what a transport can branch on: a scope failure and a
    provider failure are different things to a caller, and collapsing both into
    one "service unavailable" frame tells them the wrong one.
    """

    failure = failure_response(logger, exc, message)
    return {
        "kind": "error",
        "error": failure.error,
        "error_type": str(failure.data["error_type"]),
    }


def cited_event(
    answer: str,
    prepared: PreparedTurn,
    identity: MemoryIdentity,
    options: ChatExecutionOptions,
) -> dict[str, object]:
    """The sources the finished answer cited, as a distinct terminal event.

    The opening ``citations`` event is emitted before any text exists, so it
    can only ever say what retrieval found. This says what the answer used --
    the same thing the non-streaming ``citations`` field carries.

    A separate event kind rather than a second ``citations``: one name
    meaning "retrieved" at the start and "used" at the end would break any
    client that appends instead of overwriting, and every existing client
    keeps the semantics it was written against.
    """

    return {
        "kind": "cited_sources",
        "citations": tuple(
            citation_data(result) for result in cited_results(answer, prepared.results)
        ),
        "session_id": options.session_id,
        "project_id": identity.project_id,
    }


__all__ = ["cited_event", "error_event"]
