"""Read-only semantic expansion contracts; results remain source evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorSearchQuery, VectorSearchResult
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext

from .extraction import EvidenceSpan
from .permissions import DerivedArtifactRecord
from .records import CanonicalAssertion, CanonicalMention


class RetrievalMode(StrEnum):
    FLAT = "flat"
    LOCAL_SEMANTIC = "local_semantic"


class TopologySearchPort(Protocol):
    async def allowed_document_ids(
        self, tenant_id: str, *, access: AccessContext, limit: int = 10000
    ) -> tuple[str, ...]: ...

    async def authorized_document_ids(
        self, tenant_id: str, document_ids: tuple[str, ...], *, access: AccessContext
    ) -> set[str]: ...

    async def authorized_source_scope_ids(
        self,
        tenant_id: str,
        source_scope_ids: tuple[str, ...],
        *,
        access: AccessContext,
    ) -> set[str]: ...

    async def active_mentions(  # noqa: PLR0913 - compatible API adds mandatory serving authorization
        self,
        tenant_id: str,
        *,
        labels: tuple[str, ...] = (),
        chunk_ids: tuple[str, ...] = (),
        entity_ids: tuple[str, ...] = (),
        limit: int = 100,
        access: AccessContext | None = None,
    ) -> tuple[CanonicalMention, ...]: ...

    async def active_assertions(
        self,
        tenant_id: str,
        *,
        entity_ids: tuple[str, ...] = (),
        limit: int = 100,
        access: AccessContext | None = None,
    ) -> tuple[CanonicalAssertion, ...]: ...

    async def eligible_build_ids(
        self, tenant_id: str, build_ids: tuple[str, ...], *, access: AccessContext | None = None
    ) -> set[str]: ...

    async def eligible_artifact_ids(
        self, tenant_id: str, artifact_ids: tuple[str, ...], *, access: AccessContext | None = None
    ) -> set[str]: ...


@dataclass(frozen=True, slots=True)
class EvidencePath:
    """Retrieval association only; does not entail a composed business fact."""

    seed_chunk_id: str
    target_chunk_id: str
    entity_ids: tuple[str, ...]
    assertion_ids: tuple[str, ...] = ()
    build_ids: tuple[str, ...] = ()
    kind: str = "mention_incidence"


@dataclass(frozen=True, slots=True)
class TopologyEvidence:
    chunk_id: str
    document_id: str
    document_version_id: str
    build_ids: tuple[str, ...]
    assertion_ids: tuple[str, ...] = ()
    spans: tuple[EvidenceSpan, ...] = ()
    paths: tuple[EvidencePath, ...] = ()
    assertions: tuple[CanonicalAssertion, ...] = ()
    derived_artifact_ids: tuple[str, ...] = ()
    navigation_summaries: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class TopologyExpansion:
    evidence: tuple[TopologyEvidence, ...] = ()
    seed_entities: int = 0
    assertions: int = 0
    ambiguous_labels: int = 0
    truncated: bool = False
    suppressed_entities: int = 0


class ContextualSearchPort(Protocol):
    async def search(
        self, query: VectorSearchQuery, *, context: StorageOperationContext
    ) -> tuple[tuple[VectorSearchResult, TopologyEvidence], ...]: ...


class DerivedSearchPort(Protocol):
    async def active_derivations(
        self,
        tenant_id: str,
        *,
        access: AccessContext | None = None,
        artifact_kind: str | None = None,
        limit: int = 100,
    ) -> tuple[DerivedArtifactRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class TopologyDiagnostics:
    mode: str = "flat"
    fallback: str | None = None
    seed_entities: int = 0
    assertions: int = 0
    candidates: int = 0
    rejected: int = 0
    ambiguous_labels: int = 0
    truncated: bool = False
    suppressed_entities: int = 0
    policy_version: str | None = None
    context_tokens: int = 0
    budget_excluded: int = 0


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    original_passages: tuple[RetrievalResult, ...] = ()
    relevant_assertions: tuple[CanonicalAssertion, ...] = ()
    evidence_paths: tuple[EvidencePath, ...] = ()
    coverage_gaps: tuple[str, ...] = ()
    conflicting_evidence: tuple[tuple[str, ...], ...] = ()
    # Permission-safe hierarchy summaries are supplied by a separate lane.
    navigation_summaries: tuple[dict[str, object], ...] = ()
