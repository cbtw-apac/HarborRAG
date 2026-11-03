"""Keyset ordering and opaque cursors for connection listings.

Handles:
- the listing order: connections needing attention first, then project and ``connection_id``
- the cursor codec every ``ConnectionRepositoryPort`` implementation shares
"""

from __future__ import annotations

import base64
import json

from harborrag_core.contracts.errors import HarborValidationError

from .connection import ATTENTION_STATUSES, Connection

ConnectionSortKey = tuple[int, str, str]


def attention_rank(status: str) -> int:
    """Return 0 for statuses an operator must act on, 1 otherwise."""
    return 0 if status in ATTENTION_STATUSES else 1


def connection_sort_key(connection: Connection) -> ConnectionSortKey:
    """Return the listing order key of ``connection``."""
    return (
        attention_rank(connection.status),
        connection.project_id,
        connection.connection_id,
    )


def encode_connection_cursor(key: ConnectionSortKey) -> str:
    """Encode the last key of a page as an opaque cursor."""
    payload = json.dumps(list(key), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_connection_cursor(value: str) -> ConnectionSortKey:
    """Decode a cursor produced by ``encode_connection_cursor``.

    Args:
        value: The cursor a client sent back.

    Returns:
        The sort key the next page starts after.

    Raises:
        HarborValidationError: The cursor is not one this module produced.
    """
    padding = "=" * (-len(value) % 4)
    try:
        rank, project_id, connection_id = json.loads(base64.urlsafe_b64decode(value + padding))
        if rank not in (0, 1) or not isinstance(project_id, str) or not connection_id:
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise HarborValidationError("connection cursor is invalid") from error
    return int(rank), project_id, str(connection_id)
