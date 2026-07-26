import pytest

from harborrag_core.contracts.chunking import TextRefinementRequest, TextSplit
from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion.chunking import ChunkingError

from .chunking_helpers import make_document, make_profile, make_request, make_service


def test_service_uses_headings_as_context_without_emitting_heading_chunks() -> None:
    profile = make_profile(target=20, maximum=25)
    document = make_document(
        [
            DocumentElement("h1", "heading", "One", {"level": 1}),
            DocumentElement("p1", "paragraph", "alpha"),
            DocumentElement("p2", "paragraph", "beta"),
            DocumentElement("h2", "heading", "Two", {"level": 1}),
            DocumentElement("p3", "paragraph", "gamma"),
        ]
    )

    result = make_service(profile).chunk(make_request(document))

    assert [record.content for record in result.chunks] == ["alpha\n\nbeta", "gamma"]
    assert [record.context.structural_path for record in result.chunks] == [
        ("One",),
        ("Two",),
    ]
    assert result.chunks[0].context.structural_path == ("One",)
    assert result.chunks[0].source_span is not None
    assert result.chunks[0].source_span.source_element_ids == ("p1", "p2")
    assert result.manifest.validation.valid


def test_preferred_minimum_merges_a_small_compatible_tail_under_maximum() -> None:
    profile = make_profile(minimum=3, target=6, maximum=10)
    document = make_document(
        [
            DocumentElement("p1", "paragraph", "aaaaaa"),
            DocumentElement("p2", "paragraph", "b"),
        ]
    )

    result = make_service(profile).chunk(make_request(document))

    assert [record.content for record in result.chunks] == ["aaaaaa\n\nb"]
    assert result.chunks[0].token_count == 9
    assert len(result.chunks[0].metadata["source_units"]) == 2


def test_oversized_units_get_unique_parts_below_the_hard_maximum() -> None:
    profile = make_profile(target=3, maximum=4)
    document = make_document([DocumentElement("p1", "paragraph", "abcdefghij")])

    result = make_service(profile).chunk(make_request(document))

    assert [record.content for record in result.chunks] == ["abcd", "efgh", "ij"]
    assert all((record.token_count or 0) <= 4 for record in result.chunks)
    assert len({record.logical_chunk_id for record in result.chunks}) == 3
    assert [record.metadata["local_part_index"] for record in result.chunks] == [
        0,
        1,
        2,
    ]
    assert result.diagnostics.oversized_units == 1
    assert result.diagnostics.forced_splits == 3


def test_table_chunks_preserve_rows_and_header_metadata() -> None:
    profile = make_profile(target=8, maximum=10)
    content = "A\tB\n11\t22\n33\t44\n"
    document = make_document([DocumentElement("table-1", "table", content)])

    result = make_service(profile).chunk(make_request(document))

    assert "".join(record.content for record in result.chunks) == content
    assert len(result.chunks) == 3
    assert result.chunks[1].metadata["table_header"] == "A\tB"
    assert not result.chunks[1].content.startswith("A\tB")
    assert all(record.role == "table" for record in result.chunks)


def test_canonical_identity_separates_logical_chunk_from_revision() -> None:
    profile = make_profile(target=12, maximum=12)
    service = make_service(profile)
    first = service.chunk(make_request(make_document([DocumentElement("p1", "paragraph", "same")])))
    repeated = service.chunk(
        make_request(make_document([DocumentElement("p1", "paragraph", "same")]))
    )
    changed = service.chunk(
        make_request(make_document([DocumentElement("p1", "paragraph", "new")]))
    )

    assert first == repeated
    assert first.chunks[0].logical_chunk_id == changed.chunks[0].logical_chunk_id
    assert first.chunks[0].chunk_revision_id != changed.chunks[0].chunk_revision_id
    assert "id" not in first.chunks[0].model_fields
    assert first.chunks[0].created_at is None


def test_configuration_changes_revision_but_not_logical_identity() -> None:
    profile = make_profile(target=10, maximum=12)
    request = make_request(make_document([DocumentElement("p1", "paragraph", "content")]))

    first = make_service(profile, configuration_version="1").chunk(request)
    second = make_service(profile, configuration_version="2").chunk(request)

    assert first.chunks[0].logical_chunk_id == second.chunks[0].logical_chunk_id
    assert first.chunks[0].chunk_revision_id != second.chunks[0].chunk_revision_id
    assert first.manifest.configuration_hash != second.manifest.configuration_hash


def test_profile_limit_changes_revision_but_not_logical_identity() -> None:
    request = make_request(make_document([DocumentElement("p1", "paragraph", "content")]))

    first = make_service(make_profile(target=10, maximum=12)).chunk(request)
    second = make_service(make_profile(target=11, maximum=12)).chunk(request)

    assert first.chunks[0].logical_chunk_id == second.chunks[0].logical_chunk_id
    assert first.chunks[0].chunk_revision_id != second.chunks[0].chunk_revision_id


def test_strategy_changes_logical_identity() -> None:
    document = make_document([DocumentElement("p1", "paragraph", "content")])
    request = make_request(document)
    generic = make_service(
        make_profile(name="generic", strategy="generic", target=10, maximum=12)
    ).chunk(request)
    structured = make_service(make_profile(target=10, maximum=12)).chunk(request)

    assert generic.chunks[0].logical_chunk_id != structured.chunks[0].logical_chunk_id


def test_generic_strategy_splits_near_target_and_preserves_order() -> None:
    profile = make_profile(
        name="generic",
        strategy="generic",
        target=4,
        maximum=10,
    )
    document = make_document([DocumentElement("p1", "paragraph", "abcdefgh")])

    result = make_service(profile).chunk(make_request(document))

    assert [record.content for record in result.chunks] == ["abcd", "efgh"]
    assert [record.metadata["generic_part_index"] for record in result.chunks] == [
        0,
        1,
    ]
    assert len({record.logical_chunk_id for record in result.chunks}) == 2


def test_generic_strategy_rejects_empty_refiner_output_for_nonblank_content() -> None:
    class EmptyRefiner:
        def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
            del request
            return ()

    profile = make_profile(
        name="generic",
        strategy="generic",
        target=4,
        maximum=10,
    )
    document = make_document([DocumentElement("p1", "paragraph", "content")])

    with pytest.raises(ChunkingError, match="returned no splits"):
        make_service(profile, refiner=EmptyRefiner()).chunk(make_request(document))
