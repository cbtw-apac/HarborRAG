"""Validated decoding helpers for SQL control-plane rows."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from harborrag_core.contracts import HarborValidationError


class DatabaseRow(Protocol):
    """String-keyed row surface shared by SQLAlchemy and test mappings."""

    def __getitem__(self, key: str, /) -> Any:
        """Return one selected column value."""


def required_text(row: DatabaseRow, column: str) -> str:
    value = row[column]
    if not isinstance(value, str) or not value.strip():
        raise HarborValidationError(f"control-plane row has invalid {column}")
    return value


def optional_text(row: DatabaseRow, column: str) -> str | None:
    value = row[column]
    if value is None:
        return None
    if not isinstance(value, str):
        raise HarborValidationError(f"control-plane row has invalid {column}")
    return value


def optional_datetime(row: DatabaseRow, column: str) -> datetime | None:
    value = row[column]
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise HarborValidationError(f"control-plane row has invalid {column}")
    return value


def required_datetime(row: DatabaseRow, column: str) -> datetime:
    value = optional_datetime(row, column)
    if value is None:
        raise HarborValidationError(f"control-plane row has invalid {column}")
    return value


def required_bool(row: DatabaseRow, column: str) -> bool:
    value = row[column]
    if not isinstance(value, bool):
        raise HarborValidationError(f"control-plane row has invalid {column}")
    return value


def required_int(row: DatabaseRow, column: str) -> int:
    value = row[column]
    if isinstance(value, bool) or not isinstance(value, int):
        raise HarborValidationError(f"control-plane row has invalid {column}")
    return int(value)


def required_mapping(
    row: DatabaseRow,
    column: str,
) -> dict[str, object]:
    value = row[column]
    if not isinstance(value, Mapping):
        raise HarborValidationError(f"control-plane row has invalid {column}")
    return {str(key): item for key, item in value.items()}
