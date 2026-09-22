"""Versioned navigation cards and authoritative summary bindings (never evidence)."""

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.summary_cards import SummaryCard as SummaryCard
from harborrag_core.summary_cards import SummaryView as SummaryView
from harborrag_core.topology.extraction import digest
from harborrag_core.topology.permissions import PermissionDependency

SUMMARY_REVISION = "summary-projection-v1"
SUMMARY_PERMISSION_BLOCKERS = frozenset(
    {
        "SUMMARY_PERMISSION_SNAPSHOT_MISSING",
        "SUMMARY_PERMISSION_SNAPSHOT_UNKNOWN",
        "SUMMARY_PERMISSION_SNAPSHOT_EXPIRED",
        "SUMMARY_PROCESSING_DISALLOWED",
    }
)
SummaryKind = Literal["Structure", "DocumentVersion", "SourceEntity", "DataSource", "Tenant"]


class SummaryPolicy(StrictModel):
    revision: str = SUMMARY_REVISION
    processing_policy_revision: str | None = None
    model_fingerprint: str = Field(min_length=1)
    max_fan_in: int = Field(default=8, ge=2, le=32)
    max_input_bytes: int = Field(default=24000, ge=2048, le=30000)
    max_input_tokens: int = Field(default=6000, ge=512)
    max_calls: int = Field(default=64, ge=1, le=10000)
    debounce_seconds: float = Field(default=5, ge=0, le=300)
    max_wait_seconds: float = Field(default=60, ge=1, le=3600)
    tenant_enabled: bool = False

    @property
    def fingerprint(self) -> str:
        # Scheduling and per-run allowance do not affect the generated text.
        return digest(
            self.model_dump(
                exclude={"debounce_seconds", "max_wait_seconds", "max_calls", "tenant_enabled"}
            )
        )


class SummaryManifest(StrictModel):
    node_key: str
    kind: SummaryKind
    source_scope_id: str
    input_document_versions: dict[str, str] = Field(default_factory=dict)
    permission_dependencies: tuple[PermissionDependency, ...] = ()
    child_keys: tuple[str, ...] = ()
    child_artifact_hashes: dict[str, str] = Field(default_factory=dict)
    input_chunk_ids: tuple[str, ...] = ()
    policy_fingerprint: str
    membership_digest: str
    input_digest: str

    @property
    def binding_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


class SummaryBinding(StrictModel):
    manifest: SummaryManifest
    card: SummaryCard
    generation_key: str
    artifact_hash: str
    revision: int = Field(ge=0)
    updated_at: datetime
    coverage_mode: Literal["complete", "empty"] = "complete"

    @model_validator(mode="after")
    def verify_artifact(self) -> Self:
        if self.artifact_hash != self.card.artifact_hash:
            raise ValueError("summary artifact hash mismatch")
        return self


class SummaryLease(StrictModel):
    tenant_id: str
    source_scope_id: str
    revision: int
    fence: int
    policy: SummaryPolicy
    lease_until: datetime


class SummarySnapshot(StrictModel):
    tenant_id: str
    source_scope_id: str
    document_versions: dict[str, str]
    permission_dependencies: tuple[PermissionDependency, ...]
    membership_digest: str


def generation_key(tenant_id: str, policy: SummaryPolicy, payload: object) -> str:
    return digest([tenant_id, policy.fingerprint, payload])
