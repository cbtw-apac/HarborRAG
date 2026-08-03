"""Schemas shared by every HTTP API feature."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue


class ApiModel(BaseModel):
    """Strict base model for public request and response contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApiError(ApiModel):
    """Stable error payload returned inside an :class:`ErrorResponse`."""

    code: str
    message: str
    details: dict[str, JsonValue] | None = None
    trace_id: str | None = None


class ErrorResponse(ApiModel):
    """Envelope used by every non-success API response."""

    error: ApiError
