"""Schemas shared by every HTTP API feature.

``ApiModel`` trims surrounding whitespace from every string. A field whose
value must come back exactly as stored, such as answer text or a source item
ID that is a file path starting or ending with a space, uses ``VerbatimText``.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

VerbatimText = Annotated[str, StringConstraints(strip_whitespace=False)]


class ApiModel(BaseModel):
    """Strict base model for public request and response contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApiError(BaseModel):
    """The body of every 4xx and 5xx response."""

    code: str = Field(description="Stable code; clients branch on this, never on message")
    message: str = Field(description="Human-readable; wording may change")
    details: dict[str, Any] | list[dict[str, Any]] | None = Field(
        description="Extra context, e.g. the failing fields for 422"
    )
    trace_id: str = Field(
        pattern=r"^[0-9a-f]{32}$", description="Find this request in the logs and traces"
    )


class Error(BaseModel):
    """Envelope of every 4xx and 5xx response, and of the SSE ``error`` event."""

    error: ApiError
