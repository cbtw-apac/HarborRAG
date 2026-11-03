"""Name the dependency behind an unhandled connection failure.

Handles:
- Telling a lost connection to Postgres, Redis, Temporal, Qdrant or the graph store
  apart from a bug, from the exception chain and the modules its traceback ran through

Does NOT catch anything: the error handlers call ``failed_dependency`` on an exception
no layer translated, and answer ``503 dependency_unavailable`` when it names one.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import TracebackType

_DEPENDENCY_BY_MODULE: tuple[tuple[str, str], ...] = (
    ("asyncpg", "postgres"),
    ("psycopg", "postgres"),
    ("sqlalchemy", "postgres"),
    ("redis", "redis"),
    ("temporalio", "temporal"),
    ("qdrant_client", "qdrant"),
    ("falkordb", "graph"),
)
_CONNECTION_FAILURE_NAMES = frozenset(
    {
        "ConnectionError",
        "TimeoutError",
        "OperationalError",
        "InterfaceError",
        "ConnectionDoesNotExistError",
        "CannotConnectNowError",
        "PostgresConnectionError",
        "ResponseHandlingException",
        "RPCError",
    }
)


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield the exception and its causes, each once."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_connection_failure(exc: BaseException) -> bool:
    """Whether the exception reads as a lost or refused connection."""
    if isinstance(exc, OSError):
        return True
    return any(klass.__name__ in _CONNECTION_FAILURE_NAMES for klass in type(exc).__mro__)


def _modules(traceback: TracebackType | None) -> Iterator[str]:
    """Yield the module of each frame the traceback passed through."""
    while traceback is not None:
        yield str(traceback.tb_frame.f_globals.get("__name__", ""))
        traceback = traceback.tb_next


def _dependency_of(exc: BaseException) -> str | None:
    """Return the dependency a raising frame or the exception class belongs to."""
    modules = [type(exc).__module__, *_modules(exc.__traceback__)]
    for module in reversed(modules):
        root = module.split(".", 1)[0]
        for prefix, dependency in _DEPENDENCY_BY_MODULE:
            if root == prefix:
                return dependency
    return None


def failed_dependency(exc: BaseException) -> str | None:
    """Name the dependency whose lost connection raised ``exc``.

    Args:
        exc: An exception no service layer translated.

    Returns:
        ``postgres``, ``redis``, ``temporal``, ``qdrant`` or ``graph``; ``None`` when the
        exception is not a connection failure of one of them.
    """
    for link in _chain(exc):
        if _is_connection_failure(link) and (dependency := _dependency_of(link)) is not None:
            return dependency
    return None


__all__ = ["failed_dependency"]
