"""Turn a validated completion request into the chat service's options."""

from __future__ import annotations

from harborrag_app.api.auth.principal import Principal
from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_runtime.chat import ChatPrompt

from .filters import retrieval_filters
from .schemas import ChatCompletionRequest


class ModeUnavailableError(HarborValidationError):
    """The mode is not enabled on this install."""

    error_code = "mode_unavailable"


def chat_options(
    request: ChatCompletionRequest, principal: Principal, *, trace_id: str
) -> ChatExecutionOptions:
    """The options of one completion request.

    Args:
        request: The validated body.
        principal: The caller; its ``user_id`` owns the session.
        trace_id: The request's trace id.

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
    connections = tuple(dict.fromkeys(request.connections or ()))
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
        user_id=principal.user_id,
        mode=request.mode,
        project_ids=tuple(request.project_ids or ()),
        connections=connections,
        filters=narrowing.evidence,
        trace_id=trace_id,
    )
