from harborrag_core.domain.element import DocumentElement

from ..chunking_helpers import make_document, make_profile, make_request, make_service


def test_confluence_strategy_preserves_page_and_section_metadata() -> None:
    profile = make_profile(
        name="confluence",
        strategy="confluence",
        target=20,
        maximum=25,
    )
    document = make_document(
        [
            DocumentElement("h1", "heading", "Install", {"level": 1}),
            DocumentElement("p1", "paragraph", "Run setup"),
            DocumentElement("code-1", "code", "make install"),
        ],
        source="confluence",
        record_id="page-42",
        extra={
            "page_id": "page-42",
            "space_key": "DOCS",
            "breadcrumb": ["Home", "Documentation"],
        },
    )

    result = make_service(profile).chunk(make_request(document))

    assert [record.content for record in result.chunks] == ["Run setup", "make install"]
    assert all(record.metadata["page_id"] == "page-42" for record in result.chunks)
    assert all(record.context.structural_path == ("Install",) for record in result.chunks)
    assert result.chunks[0].context.parent_title == "Documentation"
    assert result.chunks[1].role == "code"
