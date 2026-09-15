"""Effective source permissions, resolved by trusted source-specific adapters.

These are principal decisions, not raw group lists. Adapters must resolve nested
groups, inherited denies, and ABAC before constructing a known snapshot.
"""

import json
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.ingestion import ArtifactReference
from harborrag_core.security.context import AccessContext


class PermissionDependency(StrictModel):
    resource_kind: Literal["source", "document"]
    resource_id: str = Field(min_length=1, max_length=128)
    revision: str = Field(min_length=1, max_length=128)


class ResolvedPermissionSnapshot(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    resource_kind: Literal["source", "document"]
    resource_id: str = Field(min_length=1, max_length=128)
    revision: str = Field(min_length=1, max_length=128)
    resolved_at: datetime
    expires_at: datetime
    known: bool = False
    processing_allowed: bool = False
    public: bool = False
    allowed_principal_ids: tuple[str, ...] = Field(default=(), max_length=10000)
    denied_principal_ids: tuple[str, ...] = Field(default=(), max_length=10000)

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        if self.resolved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("permission times require timezone-aware timestamps")
        if self.expires_at <= self.resolved_at:
            raise ValueError("permission snapshot expires before resolution")
        if any(
            not value or len(value) > 255
            for value in (*self.allowed_principal_ids, *self.denied_principal_ids)
        ):
            raise ValueError("resolved principal identifiers must contain 1 to 255 characters")
        return self

    def can_read(self, access: AccessContext | None, *, now: datetime) -> bool:
        if access is None or str(access.tenant_id) != self.tenant_id:
            return False
        if not self.known or now < self.resolved_at or now >= self.expires_at:
            return False
        if access.principal_id in self.denied_principal_ids:
            return False
        return self.public or access.principal_id in self.allowed_principal_ids

    @property
    def dependency(self) -> PermissionDependency:
        return PermissionDependency(
            resource_kind=self.resource_kind, resource_id=self.resource_id, revision=self.revision
        )


class BuildInputLineage(StrictModel):
    input_document_versions: dict[str, str] = Field(min_length=1, max_length=1000)
    permission_dependencies: tuple[PermissionDependency, ...] = Field(min_length=2, max_length=2000)


class DerivedArtifactLineage(BuildInputLineage):
    artifact_id: str = Field(min_length=1, max_length=128)
    artifact_kind: Literal[
        "contextual_chunk",
        "parent_summary",
        "parent_graph_view",
        "parent_description",
        "entity_view",
        "identity_proof",
    ]
    build_id: str = Field(min_length=1, max_length=128)
    input_digest: str = Field(min_length=1, max_length=128)
    metadata: dict[str, JsonValue] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def bounded_metadata(self) -> Self:
        if len(json.dumps(self.metadata).encode()) > 65536:
            raise ValueError("derived metadata exceeds 64 KiB")
        return self


class DerivedArtifactRecord(StrictModel):
    lineage: DerivedArtifactLineage
    artifact: ArtifactReference
