"""Unit tests for Jira connector ADF/content field extraction helpers."""

from __future__ import annotations

from harborrag_adapters.connectors.jira.content import (
    _display_name as content_display_name,
)
from harborrag_adapters.connectors.jira.content import _name as content_name
from harborrag_adapters.connectors.jira.content import (
    _walk_adf,
    build_raw_content,
    field_text,
)


def test_field_text_handles_none_scalar_and_plain_dict_fallback():
    assert field_text(None) == ""
    assert field_text(42) == "42"
    assert field_text({"foo": "bar"}) == "bar"


def test_field_text_extracts_html():
    assert "hi" in field_text("<p>hi</p>")


def test_field_text_keeps_html_table_rows_and_columns():
    html = (
        "<p>Pipeline</p>"
        "<table>"
        "<tr><th>Stage</th><th>Owner</th></tr>"
        "<tr><td>Interview 1</td><td>Ashley</td></tr>"
        "</table>"
    )

    assert field_text(html) == (
        "Pipeline\n\n| Stage | Owner |\n| --- | --- |\n| Interview 1 | Ashley |"
    )


def test_field_text_keeps_adf_table_rows_and_columns():
    body = _adf_document(
        {"type": "paragraph", "content": [{"type": "text", "text": "Pipeline"}]},
        _adf_table(
            [_adf_cell("Stage", header=True), _adf_cell("Owner", header=True)],
            [_adf_cell("Interview 1"), _adf_cell("Ashley")],
        ),
    )

    assert field_text(body) == (
        "Pipeline\n\n| Stage | Owner |\n| --- | --- |\n| Interview 1 | Ashley |"
    )


def test_field_text_labels_adf_table_spans():
    body = _adf_document(
        _adf_table(
            [_adf_cell("Env", header=True), _adf_cell("Region", header=True)],
            [_adf_cell("prod", row_span=2), _adf_cell("apac")],
            [_adf_cell("emea")],
        )
    )

    assert field_text(body).splitlines() == [
        "| Env | Region |",
        "| --- | --- |",
        "| prod (spans 2 rows) | apac |",
        "|  | emea |",
    ]


def test_build_raw_content_keeps_a_comment_table_in_the_rendered_issue():
    issue = {"key": "ENG-1", "fields": {"summary": "Title"}}
    comment = {
        "author": {"displayName": "Recruiter"},
        "body": _adf_document(
            _adf_table(
                [_adf_cell("Stage", header=True), _adf_cell("Result", header=True)],
                [_adf_cell("Interview 1"), _adf_cell("accepted")],
            )
        ),
    }

    content = build_raw_content(issue, comments=[comment])

    assert "| Stage | Result |" in content
    assert "| --- | --- |" in content
    assert "| Interview 1 | accepted |" in content


def test_walk_adf_handles_string_list_hardbreak_and_other_scalars():
    assert _walk_adf("plain") == ["plain"]
    assert _walk_adf([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == [
        "a",
        "b",
    ]
    assert _walk_adf({"type": "hardBreak"}) == ["\n"]
    assert _walk_adf(42) == []


def test_content_name_and_display_name_handle_missing_values():
    assert content_name(None) is None
    assert content_name({"nokey": 1}) is None
    assert content_display_name("not-a-dict") is None
    assert content_display_name({}) is None


def test_build_raw_content_skips_custom_fields_section_when_absent():
    minimal_issue = {"key": "ENG-1", "fields": {"summary": "Title"}}

    content = build_raw_content(minimal_issue)

    assert "## Custom Fields" not in content


def test_comment_author_never_lands_on_a_table_header_row():
    issue = {"key": "ENG-1", "fields": {"summary": "Title"}}
    table_comment = {
        "author": {"displayName": "Recruiter"},
        "body": _adf_document(
            _adf_table(
                [_adf_cell("Stage", header=True), _adf_cell("Result", header=True)],
                [_adf_cell("Interview 1"), _adf_cell("accepted")],
            )
        ),
    }
    prose_comment = {"author": {"displayName": "Reviewer"}, "body": "looks good"}

    content = build_raw_content(issue, comments=[table_comment, prose_comment])

    assert "Recruiter:\n| Stage | Result |" in content
    assert "Reviewer: looks good" in content


def _adf_document(*content: dict) -> dict:
    return {"type": "doc", "version": 1, "content": list(content)}


def _adf_table(*rows: list[dict]) -> dict:
    return {
        "type": "table",
        "content": [{"type": "tableRow", "content": row} for row in rows],
    }


def _adf_cell(text: str, *, header: bool = False, row_span: int = 1) -> dict:
    return {
        "type": "tableHeader" if header else "tableCell",
        "attrs": {"colspan": 1, "rowspan": row_span},
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }
