from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion.chunking import (
    ChunkingConfig,
    ChunkingRouter,
    build_default_chunking_service,
)

from .chunking_helpers import (
    CharacterCounter,
    CharacterRefiner,
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

    assert [record.role for record in result.chunks] == [
        "jira.summary",
        "jira.description",
        "jira.acceptance_criteria",
        "jira.comment",
        "jira.comment",
    ]
    assert [record.metadata.get("comment_id") for record in result.chunks[-2:]] == [
        "1001",
        "1002",
    ]
    assert result.chunks[3].metadata["author"] == "Ada"
    assert result.chunks[3].source_element_ids == ("comment-1",)


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
    assert all(record.role == "jira.comment" for record in result.chunks)
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

    assert [record.role for record in result.chunks] == [
        "jira.summary",
        "jira.description",
        "jira.comment",
        "jira.attachment",
    ]
    assert result.chunks[0].content == "HARBOR-1: HarborRAG\n\nType: Bug\nStatus: Open"
    assert result.chunks[0].structural_path == ("HarborRAG", "overview")
    assert result.chunks[1].content == (
        "Preserve Jira structure\n\nAcceptance: chunks remain stable"
    )
    assert result.chunks[2].metadata["comment_id"] == "1001"
    assert result.chunks[2].metadata["author"] == "Ada"
    assert result.chunks[3].metadata["attachment_id"] == "2001"
    assert result.chunks[3].structural_path == ("HarborRAG", "evidence.txt")
    assert "attachments" not in result.chunks[0].metadata
    assert "text" not in result.chunks[3].metadata
    assert "download_url" not in result.chunks[3].metadata


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
    assert all(record.structural_path == ("Install",) for record in result.chunks)
    assert result.chunks[0].context.parent_title == "Documentation"
    assert result.chunks[1].role == "code"


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
    assert result.chunks[0].source_element_ids == ("json-root",)


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
    assert result.chunks[0].structural_path == ("Guide", "Setup")
    assert result.chunks[0].metadata["format_fallback"] == "text/html"
    assert result.chunks[0].source_element_ids == ("html:0",)


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
    assert result.chunks[0].page_start == 1
    assert result.chunks[0].page_end == 1
    assert result.chunks[0].metadata["ocr_confidence"] == 0.93
