"""Start-up check for the MCP server.

Runs the lifespan the transport would run, then stops it. It opens no listener
and serves no request, and a failure description never repeats a rejected
setting value.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from pydantic_core import ErrorDetails

_MAX_DESCRIPTION_CHARACTERS = 300


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
    because its own text repeats the rejected values. An error raised while the
    lifespan is running arrives in an exception group, so the first member is
    described.
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
    field = ".".join(str(part) for part in detail["loc"])
    return f"{field}: {detail['msg']}" if field else detail["msg"]
