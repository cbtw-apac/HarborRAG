from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from harborrag_core.indexing import VectorSearchResult
from harborrag_core.ingestion import ActiveDocumentVersion


class ActiveVersionResolver(Protocol):
    """Resolve the Postgres-authoritative version for logical documents."""

    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> Mapping[str, ActiveDocumentVersion]: ...


@dataclass(frozen=True, slots=True)
class CandidateValidationResult:
    """Separate authoritative candidates from stale or malformed projections."""

    accepted: tuple[VectorSearchResult, ...]
    stale_count: int
    unpublished_count: int
    malformed_count: int

    @property
    def rejected_count(self) -> int:
        return self.stale_count + self.unpublished_count + self.malformed_count


class ActiveVersionCandidateValidator:
    """Validate Qdrant candidates against the Postgres publication pointer."""

    def __init__(self, resolver: ActiveVersionResolver) -> None:
        self._resolver = resolver

    async def validate(
        self,
        candidates: Sequence[VectorSearchResult],
    ) -> CandidateValidationResult:
        candidate_versions: list[tuple[VectorSearchResult, str, str]] = []
        malformed_count = 0
        for candidate in candidates:
            document_id = candidate.payload.get("document_id")
            document_version_id = candidate.payload.get("document_version_id")
            if not self._is_text(document_id) or not self._is_text(document_version_id):
                malformed_count += 1
                continue
            candidate_versions.append((candidate, str(document_id), str(document_version_id)))

        document_ids = tuple(dict.fromkeys(document_id for _, document_id, _ in candidate_versions))
        active_versions = await self._resolver.active_versions(document_ids)
        accepted: list[VectorSearchResult] = []
        stale_count = 0
        unpublished_count = 0
        for candidate, document_id, document_version_id in candidate_versions:
            active = active_versions.get(document_id)
            if active is None:
                unpublished_count += 1
            elif str(active.document_version_id) != document_version_id:
                stale_count += 1
            else:
                accepted.append(candidate)
        return CandidateValidationResult(
            accepted=tuple(accepted),
            stale_count=stale_count,
            unpublished_count=unpublished_count,
            malformed_count=malformed_count,
        )

    @staticmethod
    def _is_text(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())
