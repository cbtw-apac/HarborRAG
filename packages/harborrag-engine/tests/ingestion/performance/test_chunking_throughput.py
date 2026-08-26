"""Bounded performance regression for GraphRAG-oriented document chunking."""

from __future__ import annotations

from time import perf_counter

import pytest

from harborrag_core.domain.element import DocumentElement

from ..unit.chunking_helpers import make_document, make_profile, make_request, make_service

pytestmark = [pytest.mark.performance]


def test_document_chunking_preserves_context_at_interactive_throughput() -> None:
    """Keep large normalized documents fast without weakening chunk invariants."""

    elements: list[DocumentElement] = []
    paragraph_count = 2_000
    for index in range(paragraph_count):
        if index % 100 == 0:
            elements.append(
                DocumentElement(
                    f"heading-{index}",
                    "heading",
                    f"Subsystem {index // 100}",
                    {"level": 1},
                )
            )
        elements.append(
            DocumentElement(
                f"paragraph-{index}",
                "paragraph",
                (
                    f"Component {index} depends on service {index % 37} and emits "
                    f"event {index % 53} for downstream retrieval."
                ),
            )
        )

    service = make_service(make_profile(minimum=200, target=500, maximum=700))
    started = perf_counter()
    result = service.chunk(make_request(make_document(elements)))
    duration = perf_counter() - started

    assert duration < 5
    assert result.manifest.validation.valid
    assert result.diagnostics.source_units == paragraph_count
    assert result.manifest.total_chunk_count == len(result.chunks)
    assert all((chunk.token_count or 0) <= 700 for chunk in result.chunks)
    assert all(chunk.hierarchy.section_path for chunk in result.chunks)
