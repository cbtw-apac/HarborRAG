"""Start-up check for the MCP server.

Runs the lifespan the transport would run, then stops it. It opens no listener
and serves no request. A failure description leaves out a validation message
that repeats eight or more characters of a rejected setting value.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from pydantic_core import ErrorDetails

_MAX_DESCRIPTION_CHARACTERS = 300
_SHORTEST_REPEAT_CHARACTERS = 8


async def start_and_stop(
    transport: FastMCP[Any],
    *,
    key_store_probe: Callable[[], Awaitable[object]] | None = None,
) -> None:
    """Start the server's runtime, read the key store once if asked, then stop the runtime.

    Args:
        transport: Composed server whose lifespan connects its backends.
        key_store_probe: One read of the reader-key store, or ``None`` when keys
            are not kept in Postgres.
    """
    app = transport.http_app()
    async with app.router.lifespan_context(app):
        if key_store_probe is not None:
            await key_store_probe()


def describe_startup_failure(error: BaseException) -> str:
    """Reduce a start-up error to one line with its class and its message.

    A message that spans several lines, as a gRPC error's does, is joined, because
    its first line alone does not say what failed.
    A settings validation error lists each field and what is wrong with it,
    because its own text repeats the rejected values. A validator writes its own
    message and may quote what it rejects, so a message that shares a run of
    eight characters with a rejected value is replaced by the error type. An
    error raised while the lifespan is running arrives in an exception group, so
    the first member is described.
    """
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    if isinstance(error, ValidationError):
        problems = "; ".join(_field_problem(detail) for detail in error.errors())
        description = f"{error.title} is invalid: {problems}"
    else:
        message = " ".join(str(error).split())
        name = type(error).__name__
        description = f"{name}: {message}" if message else name
    return description[:_MAX_DESCRIPTION_CHARACTERS]


def _field_problem(detail: ErrorDetails) -> str:
    """Describe one validation error by its field and a message that quotes no input."""
    field = ".".join(str(part) for part in detail["loc"])
    message = detail["msg"]
    if _repeats_input(message, detail.get("input")):
        message = f"invalid value ({detail['type']})"
    return f"{field}: {message}" if field else message


def _repeats_input(message: str, rejected: object) -> bool:
    """Whether ``message`` contains a run of characters taken from a rejected string."""
    size = _SHORTEST_REPEAT_CHARACTERS
    return any(
        value[start : start + size] in message
        for value in _strings(rejected)
        for start in range(len(value) - size + 1)
    )


def _strings(value: object) -> Iterator[str]:
    """Yield every string in ``value``, which is a string, a mapping or a sequence."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)
