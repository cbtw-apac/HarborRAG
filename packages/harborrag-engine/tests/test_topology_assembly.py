"""Canonical semantic identity is source-backed and conservatively scoped."""

import pytest

from harborrag_core.topology import EvidenceSpan, ExtractedEntity
from harborrag_engine.topology.assembly import canonical_entity_id


def entity(name: str, entity_type: str = "system") -> ExtractedEntity:
    return ExtractedEntity(
        local_id="local",
        name=name,
        entity_type=entity_type,
        span=EvidenceSpan(start=0, end=1, quote="x"),
    )


def test_entity_identity_aggregates_unicode_case_and_whitespace_by_type() -> None:
    scope = {
        "source_scope_id": "source",
        "document_id": "document",
        "observation_scope_id": "chunk",
    }
    left = canonical_entity_id("tenant", entity("  FalkorDB  "), **scope)
    right = canonical_entity_id("tenant", entity("falkordb"), **scope)
    assert left == right
    assert left != canonical_entity_id("tenant", entity("FalkorDB", "product"), **scope)
    assert left != canonical_entity_id("other-tenant", entity("FalkorDB"), **scope)
    assert left != canonical_entity_id(
        "tenant",
        entity("FalkorDB"),
        source_scope_id="source",
        document_id="other-document",
        observation_scope_id="chunk",
    )


def test_entity_identity_does_not_guess_aliases() -> None:
    scope = {
        "source_scope_id": "source",
        "document_id": "document",
        "observation_scope_id": "chunk",
    }
    assert canonical_entity_id("tenant", entity("PostgreSQL"), **scope) != canonical_entity_id(
        "tenant", entity("Postgres"), **scope
    )


def test_ungrounded_same_name_observations_stay_distinct_until_reviewed_resolution() -> None:
    first = canonical_entity_id(
        "tenant",
        entity("Alex Kim"),
        source_scope_id="source",
        document_id="document",
        observation_scope_id="chunk-a",
    )
    second = canonical_entity_id(
        "tenant",
        entity("Alex Kim").model_copy(update={"local_id": "second"}),
        source_scope_id="source",
        document_id="document",
        observation_scope_id="chunk-b",
    )
    assert first != second


def test_ungrounded_external_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="not grounded"):
        canonical_entity_id(
            "tenant",
            entity("ACME").model_copy(update={"external_id": "svc-42"}),
            source_scope_id="source",
            document_id="document",
            observation_scope_id="chunk",
        )


def test_source_backed_external_identity_can_join_documents_without_name_guessing() -> None:
    grounded = EvidenceSpan(start=0, end=6, quote="svc-42")
    left = entity("ACME platform").model_copy(
        update={"external_id": "svc-42", "span": grounded}
    )
    right = entity("ACME").model_copy(update={"external_id": "svc-42", "span": grounded})
    assert canonical_entity_id(
        "tenant",
        left,
        source_scope_id="source",
        document_id="left",
        observation_scope_id="left-chunk",
    ) == canonical_entity_id(
        "tenant",
        right,
        source_scope_id="source",
        document_id="right",
        observation_scope_id="right-chunk",
    )
    assert canonical_entity_id(
        "tenant",
        left,
        source_scope_id="other-source",
        document_id="left",
        observation_scope_id="left-chunk",
    ) != canonical_entity_id(
        "tenant",
        right,
        source_scope_id="source",
        document_id="right",
        observation_scope_id="right-chunk",
    )
