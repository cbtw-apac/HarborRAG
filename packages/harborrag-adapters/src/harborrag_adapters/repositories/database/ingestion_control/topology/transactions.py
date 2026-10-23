"""Finish database cleanup before cancellation can start another canonical writer."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient


async def _drain[T](
    call: Awaitable[T],
    *,
    close_on_cancel: Callable[[T], None] | None = None,
) -> T:
    """Finish one database operation, then propagate pending cancellation.

    SQLAlchemy's sessionmaker context shields an exit task but does not join it
    when its caller is cancelled. A subsequent failure-recording transaction can
    then contend with the still-running cleanup. Repeated cancellation must not
    detach that task either. Database errors remain observable to the caller.
    """
    task = asyncio.ensure_future(call)
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            interrupted = True
    if interrupted:
        if close_on_cancel is not None:
            close_on_cancel(result)
        raise asyncio.CancelledError
    return result


async def _drain_exit(exit_call: Awaitable[bool | None]) -> None:
    await _drain(exit_call)


class TopologySession(AsyncSession):
    """A cancelled SQL statement completes before rollback; business code stops.

    Cancelling SQLite mid-RETURNING can leave a cursor held by the exception
    traceback after connection invalidation. Drain the buffered execution and
    close its result before cancellation reaches the transaction context.
    """

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return await _drain(
            super().execute(*args, **kwargs),
            close_on_cancel=lambda result: result.close(),
        )


@asynccontextmanager
async def read_only_transaction(client: SQLAlchemyDBClient) -> AsyncIterator[AsyncSession]:
    """A reader's transaction: one consistent snapshot, no row locks, no writes.

    Publication holds the tenant summary lock while it swaps bindings and
    scopes; a reader used to take the same lock (an INSERT plus SELECT FOR
    UPDATE) purely to avoid seeing half of that swap. A REPEATABLE READ snapshot
    gives the same guarantee without writing, so a read-only database role can
    serve every reader tool. READ ONLY makes any future write on this path fail
    loudly instead of quietly widening the role. SQLite serialises writers and
    already reads a single snapshot, so it needs nothing.
    """
    async with topology_transaction(client) as session:
        if client.raw.dialect.name == "postgresql":
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        yield session


@asynccontextmanager
async def topology_transaction(client: SQLAlchemyDBClient) -> AsyncIterator[AsyncSession]:
    """Only an in-flight SQL statement and teardown are joined, never business work."""
    factory = async_sessionmaker(client.raw, class_=TopologySession, expire_on_commit=False)
    manager = factory.begin()
    session = await manager.__aenter__()
    try:
        yield session
    except BaseException as error:
        await _drain_exit(manager.__aexit__(type(error), error, error.__traceback__))
        raise
    else:
        await _drain_exit(manager.__aexit__(None, None, None))
