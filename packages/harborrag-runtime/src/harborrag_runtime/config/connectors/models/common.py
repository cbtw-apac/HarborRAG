"""Models shared by several source types."""

from __future__ import annotations

from pydantic import Field

from .fields import StrictModel


class NoTuning(StrictModel):
    """Tuning of a source type with no HTTP client."""


class HttpTuning(StrictModel):
    """Per-connection overrides of a connector's HTTP client defaults."""

    requests_per_minute: int | None = Field(default=None, ge=1)
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    max_retries: int | None = Field(default=None, ge=0)
    backoff_factor: float | None = Field(default=None, ge=0)


class PagedHttpTuning(HttpTuning):
    """HTTP tuning for connectors that page through list endpoints."""

    page_size: int | None = Field(default=None, ge=1)


class NoCredentials(StrictModel):
    """Credentials of a source that needs none."""
