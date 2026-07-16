from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

type AsyncCloseCallback = Callable[[], Awaitable[None]]


class ResourceOwnership(StrEnum):
    """State whether a client owns or merely borrows a runtime resource."""

    OWNED = "owned"
    BORROWED = "borrowed"


@dataclass(frozen=True, slots=True)
class AsyncLifecycleResource:
    """Associate an asynchronous close callback with explicit ownership."""

    close: AsyncCloseCallback
    ownership: ResourceOwnership = ResourceOwnership.OWNED


async def close_async_resources(resources: Sequence[AsyncLifecycleResource]) -> None:
    """Close every owned resource and report failures after cleanup completes."""

    await close_async_callbacks(
        [resource.close for resource in resources if resource.ownership is ResourceOwnership.OWNED]
    )


async def close_async_callbacks(closers: Sequence[AsyncCloseCallback]) -> None:
    """Close every callback in order and report failures after cleanup completes."""

    errors: list[Exception] = []
    for closer in closers:
        try:
            await closer()
        except Exception as exc:
            errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("errors while closing model resources", errors)
