from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from harborrag_core.base import StrictModel, utc_now


class CacheEntry(StrictModel):
    """Represents cache entry data shared across HarborRAG layers."""

    key: str
    value: Any
    version: int = Field(default=1, ge=1)
    tags: set[str] = Field(default_factory=set)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None


class LockHandle(StrictModel):
    """Represents lock handle data shared across HarborRAG layers."""

    key: str
    owner_token: str
    fencing_token: int = Field(ge=1)
    expires_at: datetime
