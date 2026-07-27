from harborrag_core.domain.element import DocumentElement

from ..chunking_helpers import (
    EchoStructureSplitter,
    EmptyJsonSplitter,
    EmptyStructureSplitter,
    RootJsonSplitter,
    StaticStructureSplitter,
    make_document,
    make_profile,
    make_request,
    make_service,
)


def test_json_root_array_is_accepted_and_keeps_a_json_path() -> None:
    profile = make_profile(name="json", strategy="json", target=40, maximum=50)
    document = make_document(
        [DocumentElement("json-root", "metadata", "root")],
        content_type="application/json",
        raw={"json": [{"id": 1}, {"id": 2}]},
    )

    result = make_service(profile, json_splitter=RootJsonSplitter()).chunk(make_request(document))

    assert result.strategy == "json"
    assert result.chunks[0].metadata["json_path"] == "$['root']"
    assert result.chunks[0].source_span is not None
    assert result.chunks[0].source_span.source_element_ids == ("json-root",)


def test_document_strategy_uses_html_adapter_only_as_structure_fallback() -> None:
    profile = make_profile(target=40, maximum=50)
    document = make_document(
        [DocumentElement("html:0", "paragraph", "Flattened fallback")],
        content_type="text/html",
        raw={"html": "<h1>Guide</h1><h2>Setup</h2><p>Recovered section</p>"},
    )

    result = make_service(
        profile,
        html_splitter=StaticStructureSplitter(),
    ).chunk(make_request(document))

    assert result.chunks[0].content == "Recovered section"
    assert result.chunks[0].context.structural_path == ("Guide", "Setup")
    assert result.chunks[0].metadata["format_fallback"] == "text/html"
    assert result.chunks[0].source_span is not None
    assert result.chunks[0].source_span.source_element_ids == ("html:0",)


def test_document_fallback_reads_raw_content_for_the_selected_format() -> None:
    profile = make_profile(target=100, maximum=120)
    splitter = EchoStructureSplitter()
    document = make_document(
        [DocumentElement("html:0", "paragraph", "Normalized HTML")],
        content_type="text/html",
        raw={
            "markdown": "# Wrong format",
            "html": "<h1>Correct format</h1>",
        },
    )

    result = make_service(profile, html_splitter=splitter).chunk(make_request(document))

    assert splitter.contents == ["<h1>Correct format</h1>"]
    assert result.chunks[0].content == "<h1>Correct format</h1>"


def test_empty_document_structure_fallback_keeps_normalized_elements() -> None:
    profile = make_profile(target=40, maximum=50)
    document = make_document(
        [DocumentElement("html:0", "paragraph", "Normalized fallback")],
        content_type="text/html",
        raw={"html": "<p>Provider returned no sections</p>"},
    )

    result = make_service(
        profile,
        html_splitter=EmptyStructureSplitter(),
    ).chunk(make_request(document))

    assert [chunk.content for chunk in result.chunks] == ["Normalized fallback"]
    assert "format_fallback" not in result.chunks[0].metadata


def test_empty_json_structure_fallback_keeps_normalized_elements() -> None:
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

    result = make_service(
        profile,
        json_splitter=EmptyJsonSplitter(),
    ).chunk(make_request(document))

    assert [chunk.content for chunk in result.chunks] == ["Normalized JSON"]
    assert result.chunks[0].metadata["json_path"] == "$"
