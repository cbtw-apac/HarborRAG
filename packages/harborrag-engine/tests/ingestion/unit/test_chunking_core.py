from dataclasses import replace

import pytest

from harborrag_core.chunking import ChunkKind
from harborrag_core.contracts.chunking import TextRefinementRequest, TextSplit, TokenCounter
from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion.chunking import ChunkingError, ChunkingPlan
from harborrag_engine.ingestion.chunking.config import ChunkingProfile
from harborrag_engine.ingestion.chunking.schemas import ChunkingRequest, ChunkUnit
from harborrag_engine.ingestion.chunking.sources.canonical import (
    CanonicalDocumentChunkingStrategy,
)

from .chunking_helpers import (
    CharacterCounter,
    make_document,
    make_profile,
    make_request,
    make_service,
)


class AlternateAnchorStrategy:
    name = "alternate"
    version = "1"

    def __init__(self, token_counter: TokenCounter) -> None:
        self._canonical = CanonicalDocumentChunkingStrategy(token_counter)

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        return tuple(
            replace(unit, anchor=f"alternate:{unit.anchor}")
            for unit in self._canonical.create_units(request, profile)
        )


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
    assert [record.hierarchy.section_path for record in result.chunks] == [
        ("One",),
        ("Two",),
    ]
    assert result.chunks[0].hierarchy.section_path == ("One",)
    assert result.chunks[0].citation_locator.source_element_ids == ("p1", "p2")
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


def test_plan_soft_maximum_limits_peer_merging_below_hard_maximum() -> None:
    profile = make_profile(minimum=3, target=6, maximum=10)
    document = make_document(
        [
            DocumentElement("p1", "paragraph", "aaaaaa"),
            DocumentElement("p2", "paragraph", "b"),
        ]
    )
    plan = ChunkingPlan(
        profile="canonical",
        strategy_version="strategy-1",
        minimum_tokens=3,
        target_tokens=6,
        soft_maximum_tokens=7,
        hard_maximum_tokens=10,
    )

    result = make_service(profile).chunk(make_request(document), plan)

    assert [record.content for record in result.chunks] == ["aaaaaa", "b"]
    assert result.manifest.validation.valid


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
    assert all(record.chunk_kind == ChunkKind.TABLE for record in result.chunks)


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
    assert first.chunks[0].chunk_id != changed.chunks[0].chunk_id
    assert "id" not in first.chunks[0].model_fields
    assert "created_at" not in first.chunks[0].model_fields


def test_configuration_changes_manifest_but_not_canonical_identity() -> None:
    profile = make_profile(target=10, maximum=12)
    request = make_request(make_document([DocumentElement("p1", "paragraph", "content")]))

    first = make_service(profile, configuration_version="1").chunk(request)
    second = make_service(profile, configuration_version="2").chunk(request)

    assert first.chunks[0].logical_chunk_id == second.chunks[0].logical_chunk_id
    assert first.chunks[0].chunk_id == second.chunks[0].chunk_id
    assert first.manifest.configuration_hash != second.manifest.configuration_hash


def test_profile_limit_changes_do_not_change_identity_when_output_is_unchanged() -> None:
    request = make_request(make_document([DocumentElement("p1", "paragraph", "content")]))

    first = make_service(make_profile(target=10, maximum=12)).chunk(request)
    second = make_service(make_profile(target=11, maximum=12)).chunk(request)

    assert first.chunks[0].logical_chunk_id == second.chunks[0].logical_chunk_id
    assert first.chunks[0].chunk_id == second.chunks[0].chunk_id


def test_strategy_version_and_document_version_change_exact_chunk_identity() -> None:
    profile = make_profile(target=10, maximum=12)
    document = make_document([DocumentElement("p1", "paragraph", "content")])
    service = make_service(profile)
    first = service.chunk(
        make_request(document, document_version_id="document-version-1"),
        ChunkingPlan(
            profile="canonical",
            strategy_version="strategy-1",
            minimum_tokens=2,
            target_tokens=10,
            soft_maximum_tokens=11,
            hard_maximum_tokens=12,
        ),
    )
    changed_strategy = service.chunk(
        make_request(document, document_version_id="document-version-1"),
        ChunkingPlan(
            profile="canonical",
            strategy_version="strategy-2",
            minimum_tokens=2,
            target_tokens=10,
            soft_maximum_tokens=11,
            hard_maximum_tokens=12,
        ),
    )
    changed_document = service.chunk(
        make_request(document, document_version_id="document-version-2"),
        ChunkingPlan(
            profile="canonical",
            strategy_version="strategy-1",
            minimum_tokens=2,
            target_tokens=10,
            soft_maximum_tokens=11,
            hard_maximum_tokens=12,
        ),
    )

    assert first.chunks[0].logical_chunk_id == changed_strategy.chunks[0].logical_chunk_id
    assert first.chunks[0].logical_chunk_id == changed_document.chunks[0].logical_chunk_id
    assert first.chunks[0].chunk_id != changed_strategy.chunks[0].chunk_id
    assert first.chunks[0].chunk_id != changed_document.chunks[0].chunk_id


def test_strategy_changes_logical_identity() -> None:
    document = make_document([DocumentElement("p1", "paragraph", "content")])
    request = make_request(document)
    counter = CharacterCounter()
    alternate = make_service(
        make_profile(name="alternate", strategy="alternate", target=10, maximum=12),
        additional_strategies=(AlternateAnchorStrategy(counter),),
    ).chunk(request)
    canonical = make_service(make_profile(target=10, maximum=12)).chunk(request)

    assert alternate.chunks[0].logical_chunk_id != canonical.chunks[0].logical_chunk_id


def test_canonical_strategy_rejects_empty_oversized_refinement() -> None:
    class EmptyRefiner:
        def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
            del request
            return ()

    profile = make_profile(target=4, maximum=4)
    document = make_document([DocumentElement("p1", "paragraph", "content")])

    with pytest.raises(ChunkingError, match="returned no units"):
        make_service(profile, refiner=EmptyRefiner()).chunk(make_request(document))


def test_oversized_table_ignores_whitespace_only_refiner_splits() -> None:
    class WhitespaceRefiner:
        def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
            del request
            return (
                TextSplit(content="   ", token_count=3),
                TextSplit(content="abc", token_count=3),
            )

    profile = make_profile(target=3, maximum=3)
    document = make_document([DocumentElement("table-1", "table", "   abc")])

    result = make_service(profile, refiner=WhitespaceRefiner()).chunk(make_request(document))

    assert [record.content for record in result.chunks] == ["abc"]
