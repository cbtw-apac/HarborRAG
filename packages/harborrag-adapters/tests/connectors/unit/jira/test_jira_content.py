"""Unit tests for Jira connector ADF/content field extraction helpers."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.jira.content import (
    _display_name as content_display_name,
)
from harborrag_adapters.connectors.jira.content import _name as content_name
from harborrag_adapters.connectors.jira.content import (
    _walk_adf,
    build_raw_content,
    field_text,
)

pytestmark = pytest.mark.unit

type AdfNode = dict[str, object]


def test_field_text_handles_none_scalar_and_plain_dict_fallback() -> None:
    assert field_text(None) == ""
    assert field_text(42) == "42"
    assert field_text({"foo": "bar"}) == "bar"


def test_field_text_extracts_html() -> None:
    assert "hi" in field_text("<p>hi</p>")


def test_field_text_keeps_html_table_rows_and_columns() -> None:
    html = (
        "<p>Pipeline</p>"
        "<table>"
        "<tr><th>Stage</th><th>Owner</th></tr>"
        "<tr><td>Deploy step</td><td>Sam Sample</td></tr>"
        "</table>"
    )

    assert field_text(html) == (
        "Pipeline\n\n| Stage | Owner |\n| --- | --- |\n| Deploy step | Sam Sample |"
    )


def test_field_text_keeps_adf_table_rows_and_columns() -> None:
    body = _adf_document(
        {"type": "paragraph", "content": [{"type": "text", "text": "Pipeline"}]},
        _adf_table(
            [_adf_cell("Stage", header=True), _adf_cell("Owner", header=True)],
            [_adf_cell("Deploy step"), _adf_cell("Sam Sample")],
        ),
    )

    assert field_text(body) == (
        "Pipeline\n\n| Stage | Owner |\n| --- | --- |\n| Deploy step | Sam Sample |"
    )


def test_field_text_labels_adf_table_spans() -> None:
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


def test_field_text_adf_table_followed_by_paragraph_keeps_blank_line_between() -> None:
    body = _adf_document(
        _adf_table([_adf_cell("Stage", header=True)], [_adf_cell("Deploy step")]),
        {"type": "paragraph", "content": [{"type": "text", "text": "Signed off"}]},
    )

    assert field_text(body) == "| Stage |\n| --- |\n| Deploy step |\n\nSigned off"


def test_build_raw_content_renders_a_server_wiki_description_from_its_html() -> None:
    issue = {
        "key": "ENG-1",
        "fields": {"summary": "Title", "description": "||Stage||\n|Deploy step|"},
        "renderedFields": {
            "description": "<table><tr><th>Stage</th></tr><tr><td>Deploy step</td></tr></table>"
        },
    }

    content = build_raw_content(issue)

    assert "## Description\n| Stage |\n| --- |\n| Deploy step |" in content
    assert "||Stage||" not in content


def test_build_raw_content_keeps_a_comment_table_in_the_rendered_issue() -> None:
    issue = {"key": "ENG-1", "fields": {"summary": "Title"}}
    comment = {
        "author": {"displayName": "Alex Example"},
        "body": _adf_document(
            _adf_table(
                [_adf_cell("Stage", header=True), _adf_cell("Result", header=True)],
                [_adf_cell("Deploy step"), _adf_cell("accepted")],
            )
        ),
    }

    content = build_raw_content(issue, comments=[comment])

    assert "| Stage | Result |" in content
    assert "| --- | --- |" in content
    assert "| Deploy step | accepted |" in content


def test_walk_adf_handles_string_list_hardbreak_and_other_scalars() -> None:
    assert _walk_adf("plain") == ["plain"]
    assert _walk_adf([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == [
        "a",
        "b",
    ]
    assert _walk_adf({"type": "hardBreak"}) == ["\n"]
    assert _walk_adf(42) == []


def test_content_name_and_display_name_handle_missing_values() -> None:
    assert content_name(None) is None
    assert content_name({"nokey": 1}) is None
    assert content_display_name("not-a-dict") is None
    assert content_display_name({}) is None


def test_build_raw_content_skips_custom_fields_section_when_absent() -> None:
    minimal_issue = {"key": "ENG-1", "fields": {"summary": "Title"}}

    content = build_raw_content(minimal_issue)

    assert "## Custom Fields" not in content


def test_comment_author_never_lands_on_a_table_header_row() -> None:
    issue = {"key": "ENG-1", "fields": {"summary": "Title"}}
    table_comment = {
        "author": {"displayName": "Alex Example"},
        "body": _adf_document(
            _adf_table(
                [_adf_cell("Stage", header=True), _adf_cell("Result", header=True)],
                [_adf_cell("Deploy step"), _adf_cell("accepted")],
            )
        ),
    }
    prose_comment = {"author": {"displayName": "Sam Sample"}, "body": "looks good"}

    content = build_raw_content(issue, comments=[table_comment, prose_comment])

    assert "Alex Example:\n| Stage | Result |" in content
    assert "Sam Sample: looks good" in content
    assert "Sam Sample:\n|" not in content
    assert "Alex Example: looks good" not in content


def _adf_document(*content: AdfNode) -> AdfNode:
    return {"type": "doc", "version": 1, "content": list(content)}


def _adf_table(*rows: list[AdfNode]) -> AdfNode:
    return {
        "type": "table",
        "content": [{"type": "tableRow", "content": row} for row in rows],
    }


def _adf_cell(text: str, *, header: bool = False, row_span: int = 1) -> AdfNode:
    return {
        "type": "tableHeader" if header else "tableCell",
        "attrs": {"colspan": 1, "rowspan": row_span},
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }
