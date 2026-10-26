"""Canonical content built by the Jira-owned document transform."""

from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors.jira.document_transform import JiraDocumentTransform
from harborrag_core.domain import Document, DocumentProvenance, ParsedDocument, RawDocument
from harborrag_core.domain.element import DocumentElement


def make_inputs(
    *,
    fields: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> tuple[RawDocument, ParsedDocument, Document]:
    issue = {"key": "HARBOR-1", "fields": fields}
    raw = RawDocument(
        id="jira://HARBOR/HARBOR-1",
        source="jira",
        content="",
        content_type="jira_issue",
        metadata={"issue_key": "HARBOR-1", **(metadata or {})},
        raw=issue,
    )
    parsed = ParsedDocument(content="", parser_name="markdown")
    document = Document(
        id="jira://HARBOR/HARBOR-1",
        title="HARBOR-1",
        content=[],
        content_type="jira_issue",
        provenance=DocumentProvenance(source="jira", record_id="HARBOR-1"),
    )
    return raw, parsed, document


def test_transform_keeps_the_summary_as_canonical_evidence() -> None:
    raw, parsed, document = make_inputs(fields={"summary": "Broken login", "description": None})

    result = JiraDocumentTransform().transform(raw, parsed, document)

    assert [element.id for element in result.content] == ["jira:HARBOR-1:summary"]
    assert result.content[0].content == "HARBOR-1: Broken login"
    assert result.content[0].metadata["field"] == "summary"


def test_transform_emits_one_element_per_comment() -> None:
    raw, parsed, document = make_inputs(
        fields={"summary": "Broken login"},
        metadata={
            "comments": [
                {"id": "1001", "body": "First comment", "author": "Ada"},
                {"id": "1002", "body": "   ", "author": "Grace"},
                {"id": "1003", "body": "Third comment"},
            ]
        },
    )

    result = JiraDocumentTransform().transform(raw, parsed, document)

    comments = [element for element in result.content if element.metadata["field"] == "comment"]
    assert [element.id for element in comments] == [
        "jira:HARBOR-1:comment:1001",
        "jira:HARBOR-1:comment:1003",
    ]
    assert comments[0].content == "First comment"
    assert comments[0].metadata["comment_id"] == "1001"
    assert comments[0].metadata["author"] == "Ada"


def test_comment_only_issue_still_produces_content() -> None:
    """An issue whose prose fields are all empty must not lose its comments.

    Chunk validation resolves every chunk's source element IDs against this
    list, so an empty one fails the whole document version at ChunkAndValidate.
    """

    raw, parsed, document = make_inputs(
        fields={"summary": "Candidate profile", "description": ""},
        metadata={"comments": [{"id": "1001", "body": "Interview notes"}]},
    )

    result = JiraDocumentTransform().transform(raw, parsed, document)

    assert [element.id for element in result.content] == [
        "jira:HARBOR-1:summary",
        "jira:HARBOR-1:comment:1001",
    ]


def test_typed_custom_fields_are_rendered_as_searchable_content() -> None:
    """Non-prose custom fields hold the real record on Jira-as-a-database projects.

    They stay in `typed_custom_attributes` for filtering, but a value nobody can
    match on semantically is a value retrieval cannot reach, so they are also
    rendered once as one labelled block.
    """

    raw, parsed, document = make_inputs(
        fields={
            "summary": "Candidate profile",
            "customfield_10001": "Data Engineering",
            "customfield_10002": 3.0,
        },
        metadata={},
    )
    raw.raw["names"] = {
        "customfield_10001": "Skill Set",
        "customfield_10002": "Years of experience",
    }

    result = JiraDocumentTransform().transform(raw, parsed, document)

    block = next(
        element for element in result.content if element.metadata["field"] == "custom_fields"
    )
    assert "Skill Set: Data Engineering" in block.content
    assert "Years of experience: 3.0" in block.content
    assert [item["name"] for item in result.provenance.extra["typed_custom_attributes"]] == [
        "Skill Set",
        "Years of experience",
    ]


def test_transform_drops_table_artifacts_from_the_discarded_elements() -> None:
    """Replacing `content` orphans anything the normalizer derived from it.

    The generic normalizer builds table artifacts from the parser's rendered
    Markdown and stamps each with the `source_block_id` it came from. This
    transform replaces that element list, so carrying the artifacts forward
    leaves them pointing at elements the document no longer has, and no Jira
    strategy emits a table chunk for them. ProjectionVerifier compares the two
    sets, so the document version fails at VerifyProjections.
    """

    from harborrag_core.domain.table import TableArtifact

    artifact = TableArtifact(
        table_id="table:abc",
        table_version_id="table-version:abc",
        document_id="jira://HARBOR/HARBOR-1",
        document_version_id="document-version:1",
        source_block_id="HARBOR-1:8",
        source_version="1",
        content_hash="a" * 64,
        ordinal=0,
        row_count=1,
        column_count=1,
        column_names=["a"],
        header_row_indices=[0],
        header_hierarchy=[["a"]],
        logical_grid=[[{"cell_id": "cell-1", "inherited": False}]],
        cells=[{"cell_id": "cell-1", "row_index": 0, "column_index": 0, "text": "a"}],
    )
    raw, parsed, document = make_inputs(fields={"summary": "Broken login"})
    document = Document(
        id="jira://HARBOR/HARBOR-1",
        title=document.title,
        content=[DocumentElement("HARBOR-1:8", "table", "a")],
        content_type=document.content_type,
        provenance=document.provenance,
        table_artifacts=(artifact,),
    )

    result = JiraDocumentTransform().transform(raw, parsed, document)

    assert result.table_artifacts == ()
    assert "HARBOR-1:8" not in {element.id for element in result.content}


def test_typed_custom_fields_become_one_exact_filterable_map() -> None:
    """Each typed field lands under its normalized display name, with its own type.

    Members come from the raw value rather than the rendered text: rendering turns
    an email into a Markdown link and a user list into one multi-line string,
    neither of which an equality filter could match.
    """

    raw, parsed, document = make_inputs(
        fields={
            "summary": "Anh Nguyen",
            "customfield_1": {"value": "Data Engineering", "id": "10"},
            "customfield_2": 3.0,
            "customfield_3": [{"displayName": "Aidan NELL"}, {"displayName": "Martin PAPY"}],
            "customfield_4": False,
            "customfield_5": "2026-09-13",
            "customfield_6": "nguyen@example.com",
            "customfield_7": "A long free-text note\nthat spans lines.",
            "customfield_8": None,
        },
    )
    raw.raw["names"] = {
        "customfield_1": "Skill Set",
        "customfield_2": "Years of experience",
        "customfield_3": "Restricted to",
        "customfield_4": "PD Consent",
        "customfield_5": "Available From",
        "customfield_6": "Candidate's email 1",
        "customfield_7": "Notes",
        "customfield_8": "Empty",
    }
    raw.raw["schema"] = {
        "customfield_3": {"type": "array", "custom": "multiuserpicker"},
        "customfield_5": {"type": "date"},
    }
    raw.raw["renderedFields"] = {
        "customfield_6": '<a href="mailto:nguyen@example.com">nguyen@example.com</a>',
    }

    result = JiraDocumentTransform().transform(raw, parsed, document)

    assert result.provenance.extra["fields"] == {
        "skill_set": "Data Engineering",
        "years_of_experience": 3.0,
        "restricted_to": ["Aidan NELL", "Martin PAPY"],
        "pd_consent": False,
        "available_from": "2026-09-13",
        "candidate_s_email_1": "nguyen@example.com",
    }


def test_a_field_name_that_cannot_be_a_key_falls_back_to_the_field_id() -> None:
    """Two names that normalize alike must not overwrite each other, and a name the
    knowledge store rejects as runtime state must not fail the document version."""

    raw, parsed, document = make_inputs(
        fields={
            "summary": "Candidate",
            "customfield_1": "first",
            "customfield_2": "second",
            "customfield_3": "abc-123",
            "customfield_4": "untitled",
        },
    )
    raw.raw["names"] = {
        "customfield_1": "Rate (USD)",
        "customfield_2": "Rate USD",
        "customfield_3": "Request ID",
        "customfield_4": "???",
    }

    result = JiraDocumentTransform().transform(raw, parsed, document)

    assert result.provenance.extra["fields"] == {
        "rate_usd": "first",
        "customfield_2": "second",
        "customfield_3": "abc-123",
        "customfield_4": "untitled",
    }
