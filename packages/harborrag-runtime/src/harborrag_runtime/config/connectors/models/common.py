"""Models shared by several source types."""

from __future__ import annotations

from pydantic import Field

from .fields import StrictModel


class NoTuning(StrictModel):
    """Tuning of a source type with no HTTP client."""


class HttpTuning(StrictModel):
    """Per-connection overrides of a connector's HTTP client defaults."""

    requests_per_minute: int | None = Field(
        default=None, ge=1, description="Most requests a minute this connection sends"
    )
    request_timeout_seconds: float | None = Field(
        default=None, gt=0, description="Seconds to wait for one response"
    )
    max_retries: int | None = Field(
        default=None, ge=0, description="Retries of a failed or rate-limited request"
    )
    backoff_factor: float | None = Field(
        default=None,
        ge=0,
        description="Seconds before the first retry without Retry-After; doubles each retry",
    )


class PagedHttpTuning(HttpTuning):
    """HTTP tuning for connectors that page through list endpoints."""

    page_size: int | None = Field(default=None, ge=1, description="Items to ask for per page")


class NoCredentials(StrictModel):
    """Credentials of a source that needs none."""
