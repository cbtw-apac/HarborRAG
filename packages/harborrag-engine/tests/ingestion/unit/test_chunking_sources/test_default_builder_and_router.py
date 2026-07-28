from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion.chunking import (
    ChunkingConfig,
    ChunkingRouter,
    build_default_chunking_service,
)

from ..chunking_helpers import (
    CharacterCounter,
    CharacterRefiner,
    make_document,
    make_profile,
    make_request,
)


def test_default_builder_does_not_discover_optional_adapters() -> None:
    profile = make_profile(name="json", strategy="json", target=40, maximum=50)
    document = make_document(
        [
            DocumentElement(
                "json-root",
                "metadata",
                "Normalized JSON",
                {"json_path": "$"},
            )
        ],
        content_type="application/json",
        raw={"json": {"value": 1}},
    )

    result = build_default_chunking_service(
        config=ChunkingConfig(
            default_profile=profile.name,
            profiles={profile.name: profile},
            routes=(),
        ),
        token_counter=CharacterCounter(),
        refiner=CharacterRefiner(),
    ).chunk(make_request(document))

    assert [chunk.content for chunk in result.chunks] == ["Normalized JSON"]


def test_default_router_keeps_pdf_pages_on_the_document_strategy() -> None:
    document = make_document(
        [
            DocumentElement(
                "page-1",
                "paragraph",
                "PDF page",
                {"page": 1, "ocr_confidence": 0.93},
            )
        ],
        content_type="Application/PDF; version=1.7",
    )
    request = make_request(document)

    selected = ChunkingRouter(ChunkingConfig()).select(request)
    result = build_default_chunking_service(
        config=ChunkingConfig(),
        token_counter=CharacterCounter(),
        refiner=CharacterRefiner(),
    ).chunk(request)

    assert (selected.strategy, selected.profile) == ("document", "document")
    assert result.chunks[0].source_span is not None
    assert result.chunks[0].source_span.page_start == 1
    assert result.chunks[0].source_span.page_end == 1
    assert result.chunks[0].metadata["ocr_confidence"] == 0.93
