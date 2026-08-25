"""Strict schemas for the public connection catalog."""

from __future__ import annotations

from pydantic import Field

from harborrag_app.api.schemas import ApiModel


class ConnectionSummary(ApiModel):
    """One connection a caller may submit ingestion for."""

    connection_id: str = Field(
        min_length=1,
        max_length=255,
        description="Value to send as POST /v1/ingestions connection_id.",
    )
    source_type: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
        description="Connector provider backing the connection, e.g. confluence.",
    )


class ConnectionPage(ApiModel):
    items: list[ConnectionSummary]
