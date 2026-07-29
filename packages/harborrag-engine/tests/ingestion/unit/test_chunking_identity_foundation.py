from __future__ import annotations

import pytest

from harborrag_core.chunking import ChunkKind
from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion.chunking import (
    ChunkHierarchyError,
    ChunkHierarchyValidator,
    ChunkIdentityBuilder,
    ChunkIdentityError,
    normalize_section_path,
    parent_section_path,
)
from harborrag_engine.ingestion.chunking.identity import (
    canonical_identity_payload,
    content_fingerprint,
    encoded_identifier,
)

from .chunking_helpers import make_document, make_profile, make_request, make_service


def test_explicit_identity_vector_guards_payload_and_digest_compatibility() -> None:
    value = {"b": " Alpha\tBeta ", "a": "Cafe\u0301"}

    assert canonical_identity_payload(value) == '{"a":"Café","b":"Alpha Beta"}'
    assert encoded_identifier("vector", value) == (
        "vector:f7ff8c90ff5ca18c054330567b00a743d6cd33eff80f827fb87a3444ddb6f78d"
    )


def test_chunk_identity_vector_uses_section_range_kind_version_and_content() -> None:
    builder = ChunkIdentityBuilder()
    section_id = builder.section_id(
        document_id="document-1",
        section_path=(" Architecture ", "Chunking"),
    )
    logical_chunk_id = builder.logical_chunk_id(
        section_id=section_id,
        stable_source_range={"end": 9, "start": 2},
        chunk_kind=ChunkKind.EVIDENCE,
    )
    chunk_id = builder.chunk_id(
        logical_chunk_id=logical_chunk_id,
        document_version_id="version-1",
        strategy_version="strategy-1",
        content_hash=content_fingerprint("Alpha  beta\r\n\r\n"),
    )

    assert section_id == (
        "section:a8a37e6033c96e7c7e21b2dac5e587a9464407136e7dd27448cd71791bf9e91a"
    )
    assert logical_chunk_id == (
        "logical-chunk:5b7fb31ab21a6c8caa82c6b9aec7b82e9dc1b049d069f999bd85c8ae00dfedd0"
    )
    assert chunk_id == ("chunk:f21deae287b88ee6493961536dfc00472b38c7b8dfc237a3ab28f59f8c160cd6")
    identity = builder.identify(
        document_id="document-1",
        document_version_id="version-1",
        strategy_version="strategy-1",
        section_path=("Architecture",),
        structural_anchor="paragraph-1",
        local_part_index=0,
        chunk_kind=ChunkKind.EVIDENCE,
        content_hash="content-hash",
    )
    assert identity.chunk_revision_id == identity.chunk_id


def test_dictionary_order_whitespace_and_unicode_follow_one_policy() -> None:
    first = encoded_identifier("range", {"start": 1, "details": {"b": 2, "a": 1}})
    reordered = encoded_identifier("range", {"details": {"a": 1, "b": 2}, "start": 1})

    assert first == reordered
    assert content_fingerprint(" Café\ttext\r\n\r\n") == content_fingerprint("Cafe\u0301 text\n")
    assert content_fingerprint("Café text") != content_fingerprint("Café changed")
    with pytest.raises(ChunkIdentityError, match="kind must be non-empty"):
        encoded_identifier(" ", {"value": 1})
    with pytest.raises(ChunkIdentityError, match="finite JSON"):
        canonical_identity_payload({"score": float("nan")})
    with pytest.raises(ChunkIdentityError, match="not supported"):
        canonical_identity_payload({"values": {1, 2}})


def test_exact_chunk_identity_changes_only_for_exact_identity_inputs() -> None:
    builder = ChunkIdentityBuilder()
    common = {
        "logical_chunk_id": "logical-chunk:stable",
        "content_hash": content_fingerprint("content"),
    }
    first = builder.chunk_id(
        **common,
        document_version_id="document-version-1",
        strategy_version="strategy-1",
    )
    repeated = builder.chunk_id(
        **common,
        document_version_id="document-version-1",
        strategy_version="strategy-1",
    )
    changed_document = builder.chunk_id(
        **common,
        document_version_id="document-version-2",
        strategy_version="strategy-1",
    )
    changed_strategy = builder.chunk_id(
        **common,
        document_version_id="document-version-1",
        strategy_version="strategy-2",
    )

    assert first == repeated
    assert first != changed_document
    assert first != changed_strategy


def test_table_identity_is_stable_by_location_and_versioned_by_content() -> None:
    builder = ChunkIdentityBuilder()
    first = builder.table_id(
        document_id="document-1",
        section_path=("Tables",),
        stable_table_location={"element_id": "table-1", "ordinal": 0},
    )
    reordered = builder.table_id(
        document_id="document-1",
        section_path=("Tables",),
        stable_table_location={"ordinal": 0, "element_id": "table-1"},
    )

    assert first == reordered
    assert builder.table_version_id(
        table_id=first,
        source_version="1",
        content_hash="hash-a",
    ) != builder.table_version_id(
        table_id=first,
        source_version="1",
        content_hash="hash-b",
    )


def test_section_path_normalization_preserves_ordered_ancestry() -> None:
    path = (" Architecture ", "Cafe\u0301\tDesign ")

    assert normalize_section_path(path) == ("Architecture", "Café Design")
    assert parent_section_path(path) == ("Architecture",)
    assert parent_section_path(()) is None
    with pytest.raises(ChunkHierarchyError, match="non-empty"):
        normalize_section_path(("Architecture", " "))


def test_hierarchy_validator_rejects_duplicate_ordinals_and_unknown_neighbors() -> None:
    document = make_document(
        [
            DocumentElement("p1", "paragraph", "aaaa"),
            DocumentElement("p2", "paragraph", "bbbb"),
        ]
    )
    records = make_service(make_profile(target=4, maximum=4)).chunk(make_request(document)).chunks
    assert len(records) == 2
    validator = ChunkHierarchyValidator()

    duplicate = records[1].model_copy(update={"ordinal": records[0].ordinal})
    with pytest.raises(ChunkHierarchyError, match="duplicate ordinal"):
        validator.validate((records[0], duplicate))

    invalid_hierarchy = records[0].hierarchy.model_copy(
        update={"next_chunk_id": "logical-chunk:unknown"}
    )
    unknown = records[0].model_copy(update={"hierarchy": invalid_hierarchy})
    with pytest.raises(ChunkHierarchyError, match="unknown chunk"):
        validator.validate((unknown, records[1]))

    wrong_previous_hierarchy = records[1].hierarchy.model_copy(update={"previous_chunk_id": None})
    wrong_previous = records[1].model_copy(update={"hierarchy": wrong_previous_hierarchy})
    with pytest.raises(ChunkHierarchyError, match="previous_chunk_id"):
        validator.validate((records[0], wrong_previous))

    wrong_next_hierarchy = records[0].hierarchy.model_copy(update={"next_chunk_id": None})
    wrong_next = records[0].model_copy(update={"hierarchy": wrong_next_hierarchy})
    with pytest.raises(ChunkHierarchyError, match="next_chunk_id"):
        validator.validate((wrong_next, records[1]))
