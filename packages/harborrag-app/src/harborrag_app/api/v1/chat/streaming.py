"""One SSE contract for retrieval chat, agent runs, and completed replays."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import cast

from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.api.sse import bounded_sse_frames, sse_frame

from . import dispatch
from .replay import CompletionAttempt
from .schemas import ChatCompletionResponse, CompletionRequest

logger = logging.getLogger("harborrag.app.api.chat")

_ERRORS = {
    "ConversationTurnBusyError": (
        "conversation_turn_busy",
        "This conversation already has an active completion",
    ),
    "ConversationTurnLeaseLostError": (
        "conversation_turn_lease_lost",
        "The completion lost its conversation lease",
    ),
    "HarborNotFoundError": (
        "harbor_not_found_error",
        "Conversation session or project was not found",
    ),
    "HarborValidationError": ("harbor_validation_error", "The chat request was rejected"),
    "HarborNoIndexedContentError": (
        "no_indexed_content",
        "No content has been ingested yet, so there is nothing to search",
    ),
    "ChatStreamError": ("chat_stream_error", "The chat provider stream failed"),
}


def error_payload(event: dict[str, object]) -> dict[str, str]:
    code, message = _ERRORS.get(
        str(event.get("error_type", "")),
        ("harbor_connection_error", "Chat service is unavailable"),
    )
    return {"code": code, "message": message}


def progress_frame(event: dict[str, object]) -> bytes | None:
    """Project allowlisted service events; provider completion is not yet durable."""

    kind = event["kind"]
    if kind in {"citations", "cited_sources"}:
        return sse_frame(
            "retrieval.completed" if kind == "citations" else "response.citations",
            {key: value for key, value in event.items() if key != "kind"},
        )
    if kind == "chunk":
        chunk = cast("dict[str, object]", event["chunk"])
        if chunk.get("event") == "text_delta":
            # The UI needs display text, not provider metadata or reasoning
            # that may share an adapter chunk with the visible delta.
            return sse_frame("response.output_text.delta", {"content": chunk["content"]})
        return None
    if kind == "event":
        return sse_frame("response.agent.progress", event["event"])
    if kind == "warning":
        return sse_frame("response.warning", {"code": event["warning"]})
    return None


def stream_response(  # noqa: PLR0913 - shared stream lifecycle and replay state
    request: CompletionRequest,
    principal: Principal,
    *,
    settings: ApiSettings,
    attempt: CompletionAttempt,
    replay: ChatCompletionResponse | None = None,
    replayed: bool | None = None,
) -> StreamingResponse:
    was_replayed = replay is not None if replayed is None else replayed

    async def events() -> AsyncGenerator[bytes, None]:
        terminal = sse_frame("response.error", error_payload({"error_type": "ChatStreamError"}))
        try:
            yield sse_frame(
                "response.started",
                {
                    "session_id": replay.session_id if replay else request.session_id,
                    "mode": request.mode,
                    "replayed": was_replayed,
                },
            )
            if replay is not None:
                terminal = sse_frame("response.completed", replay.model_dump(mode="json"))
            else:
                result = None
                stream = dispatch.stream(attempt.service, request, principal, settings)
                async with contextlib.aclosing(stream):
                    async for event in stream:
                        if event["kind"] == "result":
                            result = ChatCompletionResponse.model_validate(
                                {
                                    **cast("dict[str, object]", event["result"]),
                                    "mode": request.mode,
                                }
                            )
                            break
                        if event["kind"] == "error":
                            terminal = sse_frame("response.error", error_payload(event))
                            break
                        frame = progress_frame(event)
                        if frame is not None:
                            yield frame
                # Close the service and release its lease before publishing a
                # terminal outcome. Cleanup failures cannot follow a success.
                if result is not None:
                    await attempt.finish(result)
                    terminal = sse_frame("response.completed", result.model_dump(mode="json"))
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - headers are sent; report a safe terminal frame
            logger.exception("Completion stream failed")
            terminal = sse_frame(
                "response.error", error_payload({"error_type": type(exc).__name__})
            )
        finally:
            await attempt.finish()
        yield terminal

    return StreamingResponse(
        bounded_sse_frames(
            events(),
            timeout_seconds=settings.api_stream_timeout_seconds,
            error_message="Chat stream exceeded its server deadline",
            error_event="response.error",
            terminal_events=("response.completed", "response.error"),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Idempotency-Replayed": str(was_replayed).lower(),
        },
    )
