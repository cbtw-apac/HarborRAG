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


def test_walk_adf_handles_string_list_hardbreak_and_other_scalars():
    assert _walk_adf("plain") == ["plain"]
    assert _walk_adf(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    ) == [
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
