from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion.chunking import (
    ChunkingConfig,
    ChunkValidationError,
    ChunkValidator,
)

from .chunking_helpers import (
    CharacterCounter,
    make_document,
    make_profile,
    make_request,
    make_service,
)


def test_non_serializable_metadata_is_rejected_before_indexing() -> None:
    profile = make_profile(target=10, maximum=12)
    document = make_document([DocumentElement("p1", "paragraph", "content", {"invalid": {1, 2}})])

    with pytest.raises(ChunkValidationError, match="metadata is not JSON serializable"):
        make_service(profile).chunk(make_request(document))


def test_validator_reports_blank_oversized_and_inexact_chunks() -> None:
    profile = make_profile(target=10, maximum=12)
    request = make_request(make_document([DocumentElement("p1", "paragraph", "content")]))
    record = make_service(profile).chunk(request).chunks[0]
    validator = ChunkValidator(CharacterCounter())

    blank = validator.validate(
        (record.model_copy(update={"content": " ", "token_count": 1}),),
        request,
        profile,
    )
    oversized = validator.validate(
        (record.model_copy(update={"content": "x" * 13, "token_count": 13}),),
        request,
        profile,
    )
    inexact = validator.validate(
        (record.model_copy(update={"token_count": 999}),),
        request,
        profile,
    )

    assert any("content is blank" in error for error in blank.errors)
    assert any("exceeds maximum_tokens" in error for error in oversized.errors)
    assert any("token_count is not exact" in error for error in inexact.errors)


def test_validator_reports_duplicate_ids_and_reordered_sources() -> None:
    profile = make_profile(target=10, maximum=12)
    request = make_request(
        make_document(
            [
                DocumentElement("h1", "heading", "One", {"level": 1}),
                DocumentElement("p1", "paragraph", "first"),
                DocumentElement("h2", "heading", "Two", {"level": 1}),
                DocumentElement("p2", "paragraph", "second"),
            ]
        )
    )
    records = make_service(profile).chunk(request).chunks
    validator = ChunkValidator(CharacterCounter())

    duplicate = validator.validate(
        (records[0], records[0]),
        request,
        profile,
    )
    reordered = validator.validate(
        tuple(reversed(records)),
        request,
        profile,
    )

    assert any("logical_chunk_id values must be unique" in error for error in duplicate.errors)
    assert any("chunk_id values must be unique" in error for error in duplicate.errors)
    assert any("source element order moved backward" in error for error in reordered.errors)


def test_chunk_metadata_is_recursively_immutable_and_json_serializable() -> None:
    profile = make_profile(target=10, maximum=12)
    document = make_document(
        [
            DocumentElement(
                "p1",
                "paragraph",
                "content",
                {"nested": {"values": [1, 2]}},
            )
        ]
    )

    record = make_service(profile).chunk(make_request(document)).chunks[0]

    with pytest.raises(TypeError):
        record.metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        record.metadata["nested"]["new"] = "value"
    assert '"values":[1,2]' in record.model_dump_json()


def test_manifest_contains_ordered_references_instead_of_chunk_bodies() -> None:
    profile = make_profile(target=10, maximum=12)
    result = make_service(profile).chunk(
        make_request(make_document([DocumentElement("p1", "paragraph", "content")]))
    )

    reference = result.manifest.chunks[0]
    assert result.manifest.tenant_id == "tenant-1"
    assert reference.ordinal == 0
    assert reference.chunk_id == str(result.chunks[0].chunk_id)
    assert not hasattr(reference, "content")
    assert result.manifest.total_token_count == result.chunks[0].token_count


def test_chunking_result_rejects_manifest_identity_mismatch() -> None:
    result = make_service(make_profile(target=10, maximum=12)).chunk(
        make_request(make_document([DocumentElement("p1", "paragraph", "content")]))
    )

    with pytest.raises(ValueError, match="does not match its manifest"):
        replace(result, document_version_id="another-revision")


def test_config_selects_a_source_profile_without_executing_a_strategy() -> None:
    canonical = make_profile()
    jira = make_profile(name="jira", strategy="jira")
    config = ChunkingConfig(
        default_profile="canonical",
        profiles={"canonical": canonical, "jira": jira},
        source_profiles={"jira": "jira"},
    )
    request = make_request(
        make_document(
            [DocumentElement("summary", "paragraph", "issue")],
            source="jira",
            record_id="HARBOR-1",
        )
    )

    selected = config.profile_for(request.connector_type)

    assert (selected.strategy, selected.name) == ("jira", "jira")
