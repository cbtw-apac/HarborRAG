from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from harborrag_core.base import StrictModel, utc_now
from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext


class StorageOperationStarted(StrictModel):
    """Describes the start of one sanitized storage operation."""

    family: StorageFamily
    backend: str
    operation: str
    context: StorageOperationContext
    started_at: datetime = Field(default_factory=utc_now)


class StorageOperationCompleted(StorageOperationStarted):
    """Describes a successfully completed storage operation."""

    duration_ms: float
    attributes: dict[str, Any] = Field(default_factory=dict)


class StorageOperationFailed(StorageOperationStarted):
    """Describes a failed storage operation without sensitive payloads."""

    duration_ms: float
    error_type: str
    retryable: bool
    attributes: dict[str, Any] = Field(default_factory=dict)
