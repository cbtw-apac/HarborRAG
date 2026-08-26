from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ModelMiddlewareContext:
    """Describe one model operation without exposing provider SDK objects."""

    operation: str
    logical_model: str
    model_alias: str
    request_id: str | None
    metadata: Mapping[str, Any]


class ModelMiddleware(Protocol):
    """Transform synchronous requests and responses around model execution."""

    def before_request(self, request: Any, context: ModelMiddlewareContext) -> Any:
        """Return the request that should continue through the pipeline."""

        ...

    def after_response(self, response: Any, context: ModelMiddlewareContext) -> Any:
        """Return the response exposed to the caller."""

        ...

    def on_error(self, error: Exception, context: ModelMiddlewareContext) -> None:
        """Observe an operation error without replacing it by default."""

        ...


class AsyncModelMiddleware(Protocol):
    """Transform asynchronous requests and responses around model execution."""

    async def before_request(self, request: Any, context: ModelMiddlewareContext) -> Any:
        """Return the request that should continue through the async pipeline."""

        ...

    async def after_response(self, response: Any, context: ModelMiddlewareContext) -> Any:
        """Return the response exposed to the async caller."""

        ...

    async def on_error(self, error: Exception, context: ModelMiddlewareContext) -> None:
        """Observe an async operation error without replacing it by default."""

        ...


class MiddlewarePipeline:
    """Run middleware in deterministic forward and reverse order."""

    def __init__(self, middleware: Sequence[object] = ()) -> None:
        """Store an immutable middleware sequence in registration order."""

        self._middleware = tuple(middleware)

    def before(self, request: Any, context: ModelMiddlewareContext) -> Any:
        """Apply synchronous request middleware in registration order."""

        current = request
        for item in self._middleware:
            method = getattr(item, "before_request", None)
            if method is None:
                continue
            current = _require_sync(method(current, context), "before_request")
        return current

    async def abefore(self, request: Any, context: ModelMiddlewareContext) -> Any:
        """Apply request middleware while accepting sync and async implementations."""

        current = request
        for item in self._middleware:
            method = getattr(item, "before_request", None)
            if method is None:
                continue
            current = await _await_if_needed(method(current, context))
        return current

    def after(self, response: Any, context: ModelMiddlewareContext) -> Any:
        """Apply synchronous response middleware in reverse registration order."""

        current = response
        for item in reversed(self._middleware):
            method = getattr(item, "after_response", None)
            if method is None:
                continue
            current = _require_sync(method(current, context), "after_response")
        return current

    async def aafter(self, response: Any, context: ModelMiddlewareContext) -> Any:
        """Apply response middleware in reverse order for asynchronous operations."""

        current = response
        for item in reversed(self._middleware):
            method = getattr(item, "after_response", None)
            if method is None:
                continue
            current = await _await_if_needed(method(current, context))
        return current

    def error(self, error: Exception, context: ModelMiddlewareContext) -> None:
        """Notify synchronous middleware in reverse order without masking the error."""

        hook_errors: list[Exception] = []
        for item in reversed(self._middleware):
            method = getattr(item, "on_error", None)
            if method is None:
                continue
            try:
                _require_sync(method(error, context), "on_error")
            except Exception as exc:
                hook_errors.append(exc)
        if hook_errors:
            error.add_note(_middleware_error_note(hook_errors))

    async def aerror(self, error: Exception, context: ModelMiddlewareContext) -> None:
        """Notify sync or async middleware in reverse order without masking the error."""

        hook_errors: list[Exception] = []
        for item in reversed(self._middleware):
            method = getattr(item, "on_error", None)
            if method is None:
                continue
            try:
                await _await_if_needed(method(error, context))
            except Exception as exc:
                hook_errors.append(exc)
        if hook_errors:
            error.add_note(_middleware_error_note(hook_errors))


def middleware_context(
    *, operation: str, logical_model: str, model_alias: str, request: Any
) -> ModelMiddlewareContext:
    """Build middleware context from a provider-neutral request model."""

    metadata = getattr(request, "metadata", None)
    values = (
        metadata.model_dump(mode="python")
        if metadata is not None and hasattr(metadata, "model_dump")
        else {}
    )
    return ModelMiddlewareContext(
        operation=operation,
        logical_model=logical_model,
        model_alias=model_alias,
        request_id=values.get("request_id"),
        metadata=values,
    )


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _require_sync(value: Any, method: str) -> Any:
    if inspect.isawaitable(value):
        close = getattr(value, "close", None)
        if callable(close):
            close()
        raise TypeError(f"async middleware method {method} cannot run in a sync operation")
    return value


def _middleware_error_note(errors: Sequence[Exception]) -> str:
    details = "; ".join(type(error).__name__ for error in errors)
    return f"middleware error hooks failed without replacing the original error: {details}"
