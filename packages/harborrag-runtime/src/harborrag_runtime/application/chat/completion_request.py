"""One chat completion request as the chat service takes it, and the options it narrows to.

Mode and filter refusals and the connection narrowing happen here, before any model call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from harborrag_core.contracts.errors import HarborValidationError, ModeUnavailableError
from harborrag_runtime.chat import ChatPrompt

from .options import ChatExecutionOptions
from .request_filters import FilterGroup, retrieval_filters

RequestedMode = Literal["instant", "classic", "advanced", "auto"]


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """The question of one completion request and how to narrow it.

    Args:
        message: The question.
        session_id: The session it belongs to; ``None`` asks a one-off question.
        mode: The mode asked for; only ``instant`` and ``classic`` are available.
        filters: Explorer filters to apply, if any.
        project_ids: The projects a one-off question reads.
        connections: Connection refs to narrow to.
        trace_id: Trace id of the request, recorded with the answer.
    """

    message: str
    session_id: str | None
    mode: RequestedMode
    filters: FilterGroup | None
    project_ids: tuple[str, ...]
    connections: tuple[str, ...]
    trace_id: str


@dataclass(frozen=True, slots=True)
class CompletionCaller:
    """Who asks, from verified claims.

    Args:
        tenant_id: Tenant the question is asked in.
        principal_id: Subject of the credential.
        user_id: End user who owns the session; the principal when ``None``.
    """

    tenant_id: str
    principal_id: str
    user_id: str | None


@dataclass(frozen=True, slots=True)
class ReplayKey:
    """The idempotency key of a request and the hash of its body.

    Args:
        key: ``Idempotency-Key`` of the request; ``None`` turns replay off.
        request_hash: Hash of the request body, so a key reused with another body is refused.
    """

    key: str | None
    request_hash: str


@dataclass(frozen=True, slots=True)
class CompletionOutcome:
    """A completion's stored body.

    Args:
        body: The completion as JSON, as stored for replay.
        replayed: Whether it came from the store without a model call.
    """

    body: str
    replayed: bool


def chat_options(request: CompletionRequest, *, user_id: str | None) -> ChatExecutionOptions:
    """The options of one completion request.

    Args:
        request: The question and its narrowing.
        user_id: The end user who owns the session.

    Returns:
        The chat service's options for the turn.

    Raises:
        ModeUnavailableError: ``mode`` is ``advanced`` or ``auto`` (until v2.1.0).
        HarborValidationError: ``filters`` cannot be applied, or its ``connection``
            condition excludes every requested connection (field ``filters``).
    """
    if request.mode not in ("instant", "classic"):
        raise ModeUnavailableError(
            f"mode {request.mode!r} is not available until v2.1.0", {"field": "mode"}
        )
    narrowing = retrieval_filters(request.filters)
    connections = tuple(dict.fromkeys(request.connections))
    if narrowing.connections is not None:
        connections = (
            tuple(ref for ref in connections if ref in narrowing.connections)
            if connections
            else narrowing.connections
        )
        if not connections:
            raise HarborValidationError(
                "the connection filter excludes every requested connection", {"field": "filters"}
            )
    return ChatExecutionOptions(
        session_id=request.session_id,
        system=ChatPrompt.DEFAULT,
        user_id=user_id,
        mode=request.mode,
        project_ids=request.project_ids,
        connections=connections,
        filters=narrowing.evidence,
        trace_id=request.trace_id,
    )
