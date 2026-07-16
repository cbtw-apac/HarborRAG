from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from harborrag_core.models.common.sync import run_awaitable_synchronously

type CompletionCallable = Callable[..., Any]
type AsyncCompletionCallable = Callable[..., Awaitable[Any]]


class ChatCompletionInvocation(Protocol):
    """Define the injectable LiteLLM completion boundary used by the chat client."""

    def complete(self, **kwargs: Any) -> Any:
        """Invoke a synchronous completion."""

        ...

    async def acomplete(self, **kwargs: Any) -> Any:
        """Invoke an asynchronous completion."""

        ...

    def stream(self, **kwargs: Any) -> Any:
        """Open a synchronous completion stream."""

        ...

    async def astream(self, **kwargs: Any) -> Any:
        """Open an asynchronous completion stream."""

        ...

    def close_stream(self, stream: Any) -> None:
        """Release one synchronous stream resource."""

        ...

    async def aclose_stream(self, stream: Any) -> None:
        """Release one asynchronous stream resource."""

        ...

    def close(self) -> None:
        """Release synchronous invocation resources."""

        ...

    async def aclose(self) -> None:
        """Release asynchronous invocation resources."""

        ...


class LiteLLMChatInvocation:
    """Invoke LiteLLM completion functions without exposing SDK types publicly."""

    def __init__(
        self,
        completion: CompletionCallable | None = None,
        acompletion: AsyncCompletionCallable | None = None,
    ) -> None:
        """Use injected functions or LiteLLM's default sync and async functions."""

        if completion is None or acompletion is None:
            import litellm

            completion = completion or litellm.completion
            acompletion = acompletion or litellm.acompletion
        self._completion = completion
        self._acompletion = acompletion

    def complete(self, **kwargs: Any) -> Any:
        """Invoke ``litellm.completion`` with normalized keyword arguments."""

        return self._completion(**kwargs)

    async def acomplete(self, **kwargs: Any) -> Any:
        """Invoke ``litellm.acompletion`` with normalized keyword arguments."""

        return await self._acompletion(**kwargs)

    def stream(self, **kwargs: Any) -> Any:
        """Open a stream through ``litellm.completion``."""

        return self._completion(**kwargs)

    async def astream(self, **kwargs: Any) -> Any:
        """Open a stream through ``litellm.acompletion``."""

        return await self._acompletion(**kwargs)

    def close_stream(self, stream: Any) -> None:
        """Close a synchronous stream, including LiteLLM's async-only wrapper."""

        close = getattr(stream, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                run_awaitable_synchronously(result, thread_name="harbor-chat-stream-close")
            return
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            run_awaitable_synchronously(aclose(), thread_name="harbor-chat-stream-close")

    async def aclose_stream(self, stream: Any) -> None:
        """Close an asynchronous stream, accepting sync provider fallbacks."""

        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            await aclose()
            return
        close = getattr(stream, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def close(self) -> None:
        """Complete lifecycle handling; LiteLLM owns its shared HTTP resources."""

        return None

    async def aclose(self) -> None:
        """Complete async lifecycle handling; LiteLLM owns shared HTTP resources."""

        return None


class LiteLLMChatRouterInvocation(LiteLLMChatInvocation):
    """Expose a LiteLLM Router through the stable chat invocation boundary."""

    def __init__(self, router: Any) -> None:
        self._router = router
        super().__init__(router.completion, router.acompletion)

    def close(self) -> None:
        close = getattr(self._router, "close", None)
        if callable(close):
            close()

    async def aclose(self) -> None:
        aclose = getattr(self._router, "aclose", None)
        if callable(aclose):
            await aclose()
        else:
            self.close()
