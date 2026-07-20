from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class StrictModel(BaseModel):
    """Immutable model that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtensibleModel(BaseModel):
    """Immutable model that preserves provider-specific fields."""

    model_config = ConfigDict(extra="allow", frozen=True)
