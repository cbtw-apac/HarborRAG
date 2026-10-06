"""Repeated text is collapsed with backfill, and unindexed filter keys are refused."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from test_authoritative_retrieval import ActiveVersions, SearchRepository

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.indexing import FilterOperator, VectorFilter, VectorFilterCondition
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorSearchResult
from harborrag_engine.ingestion.projections.vector import (
    EVIDENCE_PAYLOAD_INDEXES,
    SourceFieldIndex,
    VectorProjectionPolicy,
)
from harborrag_engine.retrieval import (
    ActiveVersionCandidateValidator,
    AuthoritativeProjectionSearch,
    AuthoritativeSearchRequest,
    RetrievalLane,
)
from harborrag_engine.retrieval.duplicates import collapse_duplicates
from harborrag_engine.retrieval.evidence_filters import (
    evidence_filters_schema,
    validate_evidence_filter,
    validate_evidence_filter_keys,
)


def _hit(index: int, content_hash: str | None) -> VectorSearchResult:
    payload: dict[str, object] = {
        "chunk_id": f"chunk-{index}",
        "document_id": f"document-{index}",
        "document_version_id": f"active-{index}",
    }
    if content_hash is not None:
        payload["content_hash"] = content_hash
    return VectorSearchResult(id=f"point-{index}", score=0.9, raw_score=0.9, payload=payload)


def _search(evidence: list[VectorSearchResult]) -> AuthoritativeProjectionSearch:
    return AuthoritativeProjectionSearch(
        SearchRepository(evidence),
        ActiveVersionCandidateValidator(
            ActiveVersions({f"document-{index}": f"active-{index}" for index in range(100)})
        ),
    )


def test_collapse_keeps_the_best_ranked_copy_and_never_merges_unhashed_hits() -> None:
    items = [("a", "h1"), ("b", "h1"), ("c", None), ("d", None), ("e", "h2"), ("f", "h1")]

    kept, collapsed = collapse_duplicates(items, lambda item: item[1])

    assert [name for name, _ in kept] == ["a", "c", "d", "e"]
    assert collapsed == 2


def test_duplicates_past_the_page_are_not_counted_as_collapsed() -> None:
    items = [("a", "h1"), ("b", "h2"), ("c", "h1")]

    _, collapsed = collapse_duplicates(items, lambda item: item[1], limit=2)

    assert collapsed == 0


@pytest.mark.asyncio
async def test_a_page_of_one_repeated_comment_is_backfilled_with_distinct_hits() -> None:
    """Five copies of "Bounced - Unable to Send Mailing" become one hit plus four others."""

    evidence = [
        *(_hit(index, "bounced") for index in range(5)),
        *(_hit(index, f"distinct-{index}") for index in range(5, 20)),
    ]

    result = await _search(evidence).search(
        AuthoritativeSearchRequest(lane=RetrievalLane.DENSE, top_k=5, dense_vector=(1.0,)),
        context=StorageOperationContext.system(tenant_id="tenant-1"),
    )

    assert [candidate.id for candidate in result.candidates] == [
        "point-0",
        "point-5",
        "point-6",
        "point-7",
        "point-8",
    ]
    assert result.diagnostics.collapsed_count == 4


@pytest.mark.asyncio
async def test_the_window_widens_when_it_holds_too_few_distinct_hits() -> None:
    evidence = [
        *(_hit(index, "same") for index in range(30)),
        *(_hit(index, f"distinct-{index}") for index in range(30, 40)),
    ]
    repository = SearchRepository(evidence)
    search = AuthoritativeProjectionSearch(
        repository,
        ActiveVersionCandidateValidator(
            ActiveVersions({f"document-{index}": f"active-{index}" for index in range(40)})
        ),
    )

    result = await search.search(
        AuthoritativeSearchRequest(lane=RetrievalLane.DENSE, top_k=3, dense_vector=(1.0,)),
        context=StorageOperationContext.system(tenant_id="tenant-1"),
    )

    assert [candidate.id for candidate in result.candidates] == [
        "point-0",
        "point-30",
        "point-31",
    ]
    assert [query.top_k for query, _ in repository.dense_queries][0] == 20
    assert len(repository.dense_queries) == 2


def test_indexed_keys_and_prefixed_families_are_accepted() -> None:
    validate_evidence_filter_keys(
        ["issue_key", "status", "labels", "fields.skill_set", "facet.years_experience"]
    )


@pytest.mark.parametrize(
    "key", ["document_title", "content", "fields.Skill Set", "fields.", "tenant_id"]
)
def test_an_unindexed_key_is_rejected_with_the_supported_list(key: str) -> None:
    with pytest.raises(HarborValidationError, match="unsupported evidence filter") as caught:
        validate_evidence_filter_keys([key])

    assert caught.value.details["unsupported"] == [key]
    assert "issue_key" in str(caught.value)


def test_a_range_on_an_exact_match_key_is_refused() -> None:
    with pytest.raises(HarborValidationError, match="exact-match only"):
        validate_evidence_filter(
            VectorFilter(
                must=[
                    VectorFilterCondition(
                        field="created_at",
                        operator=FilterOperator.GREATER_THAN_OR_EQUAL,
                        value=3,
                    )
                ]
            )
        )


def test_the_filters_schema_is_rendered_from_the_provisioned_indexes() -> None:
    schema = evidence_filters_schema()
    names = schema["propertyNames"]["anyOf"]  # type: ignore[index]

    assert set(names[0]["enum"]) == set(EVIDENCE_PAYLOAD_INDEXES)
    assert "tenant_id" not in names[0]["enum"]
    assert [item["pattern"] for item in names[1:]] == [
        r"^fields\.[a-z0-9][a-z0-9_]{0,127}$",
        r"^facet\.[a-z][a-z0-9_]{0,63}$",
    ]


def test_the_filters_schema_accepts_indexed_and_prefixed_keys_only() -> None:
    validator = Draft202012Validator(evidence_filters_schema())

    assert validator.is_valid({"status": "Done", "labels": ["a", "b"]})
    assert validator.is_valid({"fields.years_of_experience": {"gte": 5}})
    assert validator.is_valid({"facet.skill_set": ["BE-Java"]})
    assert not validator.is_valid({"category": "architecture"})
    assert not validator.is_valid({"tenant_id": "AUTA-5"})


def test_provisioning_indexes_the_tenants_declared_source_fields() -> None:
    policy = VectorProjectionPolicy(
        dimension=3,
        field_indexes={
            "AUTA-5": (
                SourceFieldIndex("fields.skill_set"),
                SourceFieldIndex("fields.years_of_experience", numeric=True),
            )
        },
    )

    spec = policy.index("evidence", tenant_id="AUTA-5")
    other = policy.index("evidence", tenant_id="OTHER")

    assert spec.metadata_indexes[: len(EVIDENCE_PAYLOAD_INDEXES)] == list(EVIDENCE_PAYLOAD_INDEXES)
    assert spec.metadata_indexes[len(EVIDENCE_PAYLOAD_INDEXES) :] == [
        "fields.skill_set",
        "fields.years_of_experience",
    ]
    assert spec.float_metadata_indexes == ["fields.years_of_experience"]
    assert other.metadata_indexes == list(EVIDENCE_PAYLOAD_INDEXES)
