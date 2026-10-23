"""Canonical topology jobs and independently accepted builds."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from harborrag_core.base import StrictModel
from harborrag_core.ingestion import ArtifactReference

from .derived import ChunkEnrichment, ContextualManifest
from .extraction import ExtractedAssertion, ExtractedEntity, ExtractionProfile, digest
from .permissions import PermissionDependency
from .revisions import CONSERVATIVE_RESOLUTION_REVISION


class TopologyPolicy(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    source_scope_id: str = Field(min_length=1, max_length=128)
    enabled: bool = False
    profile: ExtractionProfile
    resolution_revision: str = CONSERVATIVE_RESOLUTION_REVISION
    projection_revision: str = "canonical-v1"
    max_attempts: int = Field(default=3, ge=1, le=10)

    @property
    def fingerprint(self) -> str:
        return digest(
            {
                "extraction": self.profile.fingerprint,
                "resolution": self.resolution_revision,
                "projection": self.projection_revision,
            }
        )


class TopologyJob(StrictModel):
    job_id: str
    tenant_id: str
    source_scope_id: str
    document_id: str
    document_version_id: str
    policy: TopologyPolicy
    policy_revision: int
    state: Literal["pending", "running", "accepted", "failed", "superseded", "deferred"]
    config_epoch: int = 0
    permission_dependencies: tuple[PermissionDependency, ...] = ()
    fence: int = 0
    attempts: int = 0
    lease_until: datetime | None = None
    available_at: datetime | None = None
    error_code: str | None = None


class ChunkExtractionCheckpoint(StrictModel):
    chunk_id: str
    input_digest: str
    artifact: ArtifactReference
    deployment_revision: str


class CanonicalMention(StrictModel):
    mention_id: str
    entity_id: str
    tenant_id: str
    build_id: str
    document_id: str
    document_version_id: str
    chunk_id: str
    observation: ExtractedEntity


class CanonicalAssertion(StrictModel):
    assertion_id: str
    tenant_id: str
    build_id: str
    document_id: str
    document_version_id: str
    chunk_id: str
    subject_entity_id: str
    object_entity_id: str
    observation: ExtractedAssertion


class TopologyBuildContent(StrictModel):
    build_id: str
    job_id: str
    document_id: str = ""
    document_version_id: str = ""
    source_scope_id: str = ""
    config_epoch: int = 0
    permission_dependencies: tuple[PermissionDependency, ...] = ()
    chunk_ids: tuple[str, ...] = Field(max_length=10000)
    mentions: tuple[CanonicalMention, ...] = Field(default=(), max_length=100000)
    assertions: tuple[CanonicalAssertion, ...] = Field(default=(), max_length=100000)
    representations: tuple[ChunkEnrichment, ...] = Field(default=(), max_length=10000)
    contextual_manifest: ContextualManifest | None = None
    projection_revision: str = "semantic-v1"


class DocumentTopologyBuild(TopologyBuildContent):
    artifact: ArtifactReference
