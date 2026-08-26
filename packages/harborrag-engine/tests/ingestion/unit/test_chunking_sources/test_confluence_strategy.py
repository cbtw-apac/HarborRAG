from harborrag_core.chunking import ChunkKind
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
    assert all(record.hierarchy.section_path == ("Install",) for record in result.chunks)
    assert result.chunks[0].hierarchy.parent_title == "Documentation"
    assert result.chunks[1].chunk_kind == ChunkKind.CODE


def test_confluence_comments_and_tabs_remain_independent_evidence_units() -> None:
    profile = make_profile(
        name="confluence",
        strategy="confluence",
        minimum=80,
        target=160,
        maximum=200,
    )
    document = make_document(
        [
            DocumentElement("h1", "heading", "Limits", {"level": 1}),
            DocumentElement(
                "tab-evidence",
                "paragraph",
                "Production uses four workers.",
                {
                    "section_path": ("Limits", "Workers"),
                    "tab_path": ("Production",),
                },
            ),
            DocumentElement(
                "fallback",
                "paragraph",
                "Fallback outside the tab.",
                {"section_path": ("Limits",), "tab_path": ()},
            ),
            DocumentElement("comments", "heading", "Comments", {"level": 1}),
            DocumentElement(
                "comment-1",
                "paragraph",
                "First comment.",
                {"role": "confluence.comment", "comment_id": "comment-1"},
            ),
            DocumentElement(
                "comment-2",
                "paragraph",
                "Reply comment.",
                {"role": "confluence.comment", "comment_id": "comment-2"},
            ),
        ],
        source="confluence",
        record_id="page-42",
        extra={"page_id": "page-42"},
    )

    result = make_service(profile).chunk(make_request(document))

    assert [record.content for record in result.chunks] == [
        "Production uses four workers.",
        "Fallback outside the tab.",
        "First comment.",
        "Reply comment.",
    ]
    assert result.chunks[0].hierarchy.section_path == (
        "Limits",
        "Production",
        "Workers",
    )
    assert result.chunks[1].hierarchy.section_path == ("Limits",)
    assert all(record.chunk_kind == ChunkKind.COMMENT for record in result.chunks[2:])
