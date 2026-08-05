from __future__ import annotations

from math import isfinite

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import DocumentId

SourceAttributeScalar = str | int | float | bool | None


def _validate_range(label: str, start: int | None, end: int | None) -> None:
    if (start is None) != (end is None):
        raise ValueError(f"{label} bounds must be provided together")
    if start is not None and end is not None and end < start:
        raise ValueError(f"{label} end must not precede start")


class SourceLocator(StrictModel):
    """Framework-neutral location of evidence in normalized source content."""

    uri: str | None = None
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    page_start: int | None = Field(default=None, ge=0)
    page_end: int | None = Field(default=None, ge=0)
    source_element_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_location(self) -> SourceLocator:
        """Reject incomplete ranges and blank location values."""

        _validate_range("offset", self.start_offset, self.end_offset)
        _validate_range("line", self.start_line, self.end_line)
        _validate_range("page", self.page_start, self.page_end)
        if self.uri is not None and not self.uri.strip():
            raise ValueError("source locator uri must be non-empty when provided")
        if any(not element_id.strip() for element_id in self.source_element_ids):
            raise ValueError("source_element_ids must be non-empty")
        return self


class ChunkSecurity(StrictModel):
    """Opaque permission identity without credentials or raw access-control entries."""

    permission_set_id: str = Field(min_length=1)
    inherited_from_document_id: DocumentId | None = None
    visibility: str | None = None

    @field_validator("permission_set_id")
    @classmethod
    def validate_permission_set_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("permission_set_id must be non-empty")
        return value

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("visibility must be non-empty when provided")
        return value


class SourceAttribute(StrictModel):
    """One relevant source field with its display-name snapshot."""

    key: str = Field(min_length=1)
    value: SourceAttributeScalar | tuple[SourceAttributeScalar, ...]
    display_name: str | None = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source attribute key must be non-empty")
        return value

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("source attribute display_name must be non-empty when provided")
        return value

    @field_validator("value")
    @classmethod
    def validate_value(
        cls,
        value: SourceAttributeScalar | tuple[SourceAttributeScalar, ...],
    ) -> SourceAttributeScalar | tuple[SourceAttributeScalar, ...]:
        values = value if isinstance(value, tuple) else (value,)
        if any(isinstance(item, float) and not isfinite(item) for item in values):
            raise ValueError("source attribute values must be finite")
        return value
