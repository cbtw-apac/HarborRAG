from __future__ import annotations

import pytest

from harborrag_core.ingestion import ActiveDocumentVersion
from harborrag_core.schemas.vector import VectorSearchResult
from harborrag_engine.retrieval import ActiveVersionCandidateValidator


class StubActiveVersionResolver:
    def __init__(self) -> None:
        self.requests: list[tuple[str, ...]] = []

    async def active_versions(
        self,
        document_ids: tuple[str, ...],
    ) -> dict[str, ActiveDocumentVersion]:
        self.requests.append(document_ids)
        return {
            "document-active": ActiveDocumentVersion(
                document_id="document-active",
                document_version_id="version-active",
            ),
            "document-stale": ActiveDocumentVersion(
                document_id="document-stale",
                document_version_id="version-new",
            ),
        }


@pytest.mark.asyncio
async def test_validator_bulk_resolves_and_rejects_stale_projection_records() -> None:
    resolver = StubActiveVersionResolver()
    validator = ActiveVersionCandidateValidator(resolver)

    result = await validator.validate(
        (
            _candidate("accepted", "document-active", "version-active"),
            _candidate("stale", "document-stale", "version-old"),
            _candidate("unpublished", "document-unpublished", "version-candidate"),
            VectorSearchResult(
                id="malformed",
                score=0.4,
                raw_score=0.4,
                payload={"document_id": "document-active"},
            ),
        )
    )

    assert [candidate.id for candidate in result.accepted] == ["accepted"]
    assert result.stale_count == 1
    assert result.unpublished_count == 1
    assert result.malformed_count == 1
    assert result.rejected_count == 3
    assert resolver.requests == [("document-active", "document-stale", "document-unpublished")]


def _candidate(
    point_id: str,
    document_id: str,
    document_version_id: str,
) -> VectorSearchResult:
    return VectorSearchResult(
        id=point_id,
        score=0.5,
        raw_score=0.5,
        payload={
            "document_id": document_id,
            "document_version_id": document_version_id,
        },
    )
