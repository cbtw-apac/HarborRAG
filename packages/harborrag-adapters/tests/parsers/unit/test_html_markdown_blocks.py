"""White-box unit tests for HTML-to-Markdown block, list, inline, and fallback rendering."""

from __future__ import annotations

import sys

import pytest

from harborrag_adapters.parsers.common.html_markdown import (
    MARKDOWN_ENGINE,
    _render_fragment,
    html_to_markdown,
    html_to_markdown_with_engine,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_without_beautifulsoup_rendering_degrades_to_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "bs4", None)

    output, engine = html_to_markdown_with_engine(b"<h1>Title</h1><p>Body text</p>")

    assert engine == "python/html.parser"
    assert engine != MARKDOWN_ENGINE
    assert "Title" in output
    assert "Body text" in output
    assert "#" not in output


def test_inline_text_beside_a_nested_block_keeps_its_position() -> None:
    html = "<div>Intro <b>bold</b><p>Para</p></div><p></p><div><p></p></div>"

    assert html_to_markdown(html) == "Intro **bold**\n\nPara"


def test_quotes_rules_and_code_blocks_render_as_markdown() -> None:
    html = "<blockquote>line one<br>line two</blockquote><hr><pre>\n\n</pre><pre>x = 1</pre>"

    assert html_to_markdown(html).splitlines() == [
        "> line one",
        "> line two",
        "",
        "---",
        "",
        "```text",
        "x = 1",
        "```",
    ]


def test_lists_keep_nesting_numbering_and_loose_blocks() -> None:
    html = (
        "<ul><li></li><li>a<ul><li>nested</li></ul></li><br>"
        "<div><p>loose</p></div><div></div><li>b<div><p>deep</p></div></li></ul>"
        "<ol><li>one</li><li>two</li></ol>"
    )

    assert html_to_markdown(html).splitlines() == [
        "- a",
        "  - nested",
        "",
        "loose",
        "- b",
        "  deep",
        "",
        "1. one",
        "2. two",
    ]


def test_inline_emphasis_code_links_and_images() -> None:
    html = (
        '<p><em>e</em> <i>i</i> <code>a`b</code> <a>bare</a> <img alt="chart" src="c.png"> '
        "<img> <strong></strong></p>"
    )

    assert html_to_markdown(html) == "*e* *i* `a\\`b` bare ![chart](c.png) image"


def test_nested_tables_render_after_the_outer_grid_and_lists_stay_in_cells() -> None:
    html = (
        "<table></table><table><tr><td>outer<table><tr><td>inner</td></tr></table></td>"
        "<td><ul><li>x</li><li>y</li></ul></td></tr></table>"
    )

    assert html_to_markdown(html).splitlines() == [
        "| Column 1 | Column 2 |",
        "| --- | --- |",
        "| outer | - x<br>- y |",
        "",
        "**Nested table 1**",
        "",
        "| Column 1 |",
        "| --- |",
        "| inner |",
    ]


def test_table_inside_a_list_item_is_indented_under_it() -> None:
    html = "<ul><li>item<table><tr><td><p>cell</p></td></tr></table></li></ul>"

    assert html_to_markdown(html).splitlines() == [
        "- item",
        "  | Column 1 |",
        "  | --- |",
        "  | cell |",
    ]


def test_unknown_heading_level_is_plain_text() -> None:
    assert html_to_markdown("<h7>x</h7><h3>Three</h3>") == "x\n\n### Three"


def test_rendering_a_block_root_renders_that_block() -> None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup("<h2>Root heading</h2>", "html.parser")

    assert _render_fragment(soup.h2) == "## Root heading"


def test_list_skips_whitespace_scripts_and_empty_nested_blocks() -> None:
    html = (
        "<script>x()</script><ul> <div><p>first</p></div><li><b>z</b></li> "
        "<li>a<ul></ul><div><p></p></div></li> </ul>"
    )

    assert html_to_markdown(html).splitlines() == ["first", "- **z**", "- a"]
