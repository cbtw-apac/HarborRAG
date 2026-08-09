"""Schemas shared by every HTTP API feature."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


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


class IngestionWorkflowInput(ApiModel):
    """Deprecated Temporal-run submission contract."""

    tenant_id: str = Field(min_length=1, max_length=255)
    connector_name: str = Field(min_length=1, max_length=100)
    run_id: str | None = Field(default=None, min_length=1, max_length=512)
    manifest_id: str | None = Field(default=None, min_length=1, max_length=512)
    generation_id: str | None = Field(default=None, min_length=1, max_length=512)
    max_artifacts: int | None = Field(default=None, ge=1)
    wait: bool = False


class IngestionControlInput(ApiModel):
    """Deprecated Temporal-run control contract."""

    action: Literal["pause", "resume", "cancel", "retry"]
    artifact_ids: list[str] = Field(default_factory=list, max_length=1_000)
    graceful: bool = True

    @model_validator(mode="after")
    def validate_artifact_ids(self) -> Self:
        if self.action == "retry" and not self.artifact_ids:
            raise ValueError("artifact_ids is required when action is retry")
        if self.action != "retry" and self.artifact_ids:
            raise ValueError("artifact_ids is only supported when action is retry")
        if any(not artifact_id.strip() for artifact_id in self.artifact_ids):
            raise ValueError("artifact_ids cannot contain blank values")
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("artifact_ids cannot contain duplicate values")
        return self
