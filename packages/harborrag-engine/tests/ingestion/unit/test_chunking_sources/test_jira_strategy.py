from harborrag_core.chunking import ChunkKind
from harborrag_core.domain.element import DocumentElement

from ..chunking_helpers import make_document, make_profile, make_request, make_service


def test_jira_strategy_preserves_fields_and_comment_boundaries() -> None:
    profile = make_profile(name="jira", strategy="jira", target=20, maximum=25)
    document = make_document(
        [
            DocumentElement("summary", "paragraph", "A bug", {"field": "summary"}),
            DocumentElement(
                "description",
                "paragraph",
                "Steps to reproduce",
                {"field": "description"},
            ),
            DocumentElement(
                "acceptance",
                "paragraph",
                "It is fixed",
                {"field": "acceptance_criteria"},
            ),
            DocumentElement(
                "comment-1",
                "paragraph",
                "First comment",
                {"field": "comment", "comment_id": "1001", "author": "Ada"},
            ),
            DocumentElement(
                "comment-2",
                "paragraph",
                "Second comment",
                {"field": "comment", "comment_id": "1002"},
            ),
        ],
        source="jira",
        record_id="HARBOR-1",
        extra={"issue_key": "HARBOR-1", "project_key": "HARBOR"},
    )

    result = make_service(profile).chunk(make_request(document))

    assert [record.chunk_kind for record in result.chunks] == [
        ChunkKind.TEXT,
        ChunkKind.TEXT,
        ChunkKind.TEXT,
        ChunkKind.COMMENT,
        ChunkKind.COMMENT,
    ]
    assert [record.metadata.get("comment_id") for record in result.chunks[-2:]] == [
        "1001",
        "1002",
    ]
    assert result.chunks[3].metadata["author"] == "Ada"
    assert result.chunks[3].citation_locator.source_element_ids == ("comment-1",)


def test_long_jira_comment_splits_only_inside_its_stable_anchor() -> None:
    profile = make_profile(
        name="jira",
        strategy="jira",
        target=3,
        maximum=4,
    )
    document = make_document(
        [
            DocumentElement(
                "comment-1",
                "paragraph",
                "abcdefghij",
                {"field": "comment", "comment_id": "1001"},
            )
        ],
        source="jira",
        record_id="HARBOR-1",
        extra={"issue_key": "HARBOR-1"},
    )

    result = make_service(profile).chunk(make_request(document))

    assert [record.content for record in result.chunks] == ["abcd", "efgh", "ij"]
    assert all(record.chunk_kind == ChunkKind.COMMENT for record in result.chunks)
    assert all(record.metadata["comment_id"] == "1001" for record in result.chunks)
    assert [record.metadata["local_part_index"] for record in result.chunks] == [
        0,
        1,
        2,
    ]


def test_jira_connector_markdown_recovers_fields_and_child_entity_boundaries() -> None:
    profile = make_profile(name="jira", strategy="jira", target=100, maximum=120)
    document = make_document(
        [
            DocumentElement("h1", "heading", "HARBOR-1 Fix ingestion", {"level": 1}),
            DocumentElement("metadata", "paragraph", "Type: Bug\nStatus: Open"),
            DocumentElement("h2-description", "heading", "Description", {"level": 2}),
            DocumentElement("description", "paragraph", "Preserve Jira structure"),
            DocumentElement("h2-custom", "heading", "Custom Fields", {"level": 2}),
            DocumentElement("custom", "paragraph", "Acceptance: chunks remain stable"),
            DocumentElement("h2-comments", "heading", "Comments", {"level": 2}),
            DocumentElement("rendered-comment", "paragraph", "Ada: Looks good"),
            DocumentElement("h2-attachments", "heading", "Attachments", {"level": 2}),
            DocumentElement("h3-attachment", "heading", "evidence.txt", {"level": 3}),
            DocumentElement("rendered-attachment", "paragraph", "attachment evidence"),
        ],
        source="jira",
        record_id="HARBOR-1",
        extra={
            "source_system": "jira",
            "issue_key": "HARBOR-1",
            "project_key": "HARBOR",
            "comments": [
                {
                    "id": "1001",
                    "author": "Ada",
                    "body": "Looks good",
                }
            ],
            "attachments": [
                {
                    "id": "2001",
                    "title": "evidence.txt",
                    "status": "processed",
                    "text": "attachment evidence",
                    "download_url": "https://jira.example.test/secret",
                },
                {
                    "id": "2002",
                    "title": "failed.pdf",
                    "status": "failed",
                    "text": None,
                },
            ],
        },
    )

    result = make_service(profile).chunk(make_request(document))

    assert [record.chunk_kind for record in result.chunks] == [
        ChunkKind.TEXT,
        ChunkKind.TEXT,
        ChunkKind.COMMENT,
        ChunkKind.TEXT,
    ]
    assert result.chunks[0].content == "HARBOR-1: HarborRAG\n\nType: Bug\nStatus: Open"
    assert result.chunks[0].hierarchy.section_path == (
        "HarborRAG",
        "overview",
    )
    assert result.chunks[1].content == (
        "Preserve Jira structure\n\nAcceptance: chunks remain stable"
    )
    assert result.chunks[2].metadata["comment_id"] == "1001"
    assert result.chunks[2].metadata["author"] == "Ada"
    assert result.chunks[3].metadata["attachment_id"] == "2001"
    assert result.chunks[3].hierarchy.section_path == (
        "HarborRAG",
        "evidence.txt",
    )
    assert "attachments" not in result.chunks[0].metadata
    assert "text" not in result.chunks[3].metadata
    assert "download_url" not in result.chunks[3].metadata
