from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

type SyncCloseCallback = Callable[[], None]
type AsyncCloseCallback = Callable[[], Awaitable[None]]


class ResourceOwnership(StrEnum):
    """State whether a client owns or merely borrows a runtime resource."""

    OWNED = "owned"
    BORROWED = "borrowed"


@dataclass(frozen=True, slots=True)
class LifecycleResource:
    """Associate a synchronous close callback with explicit ownership."""

    close: SyncCloseCallback
    ownership: ResourceOwnership = ResourceOwnership.OWNED


@dataclass(frozen=True, slots=True)
class AsyncLifecycleResource:
    """Associate an asynchronous close callback with explicit ownership."""

    close: AsyncCloseCallback
    ownership: ResourceOwnership = ResourceOwnership.OWNED


def close_resources(resources: Sequence[LifecycleResource]) -> None:
    """Close every owned synchronous resource and aggregate cleanup failures."""

    close_callbacks(
        [resource.close for resource in resources if resource.ownership is ResourceOwnership.OWNED]
    )


def close_callbacks(closers: Sequence[SyncCloseCallback]) -> None:
    """Run every synchronous closer before reporting any cleanup failure."""

    errors: list[Exception] = []
    for closer in closers:
        try:
            closer()
        except Exception as exc:
            errors.append(exc)
    _raise_cleanup_errors(errors)


async def close_async_resources(resources: Sequence[AsyncLifecycleResource]) -> None:
    """Close every owned async resource and report failures after cleanup completes."""

    await close_async_callbacks(
        [resource.close for resource in resources if resource.ownership is ResourceOwnership.OWNED]
    )


async def close_async_callbacks(closers: Sequence[AsyncCloseCallback]) -> None:
    """Close every asynchronous callback before reporting cleanup failures."""

    errors: list[Exception] = []
    for closer in closers:
        try:
            await closer()
        except Exception as exc:
            errors.append(exc)
    _raise_cleanup_errors(errors)


def _raise_cleanup_errors(errors: Sequence[Exception]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("errors while closing model resources", list(errors))
