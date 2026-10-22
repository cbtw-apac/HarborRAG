"""Shared error mapping for control-plane stores that back an auth decision."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

from harborrag_core.ports.api_keys import AuthStoreUnavailable


@contextmanager
def store_errors(message: str) -> Generator[None]:
    """Turn any failure to reach or query the store into ``AuthStoreUnavailable``.

    ``IntegrityError`` passes through: a rejected row is a bad write (a duplicate
    key id, a value outside the constraints), not an outage, and must not be
    retried as one.
    """

    try:
        yield
    except IntegrityError:
        raise
    except Exception as exc:  # noqa: BLE001 - every other failure here means "cannot decide"
        # SQLAlchemyError covers query failures, but a refused connection or a
        # rejected password surfaces as OSError/asyncpg errors that SQLAlchemy
        # re-raises raw. Whatever it was, the answer is the same: deny, and let
        # the caller tell an outage from a bad key by the exception type.
        raise AuthStoreUnavailable(message) from exc


__all__ = ["store_errors"]
