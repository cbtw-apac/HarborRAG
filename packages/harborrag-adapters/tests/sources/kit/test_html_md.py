from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.sources._kit.render import (
    Block,
    HtmlMarkdownRenderer,
    HtmlNode,
    RenderContext,
    parse_html,
)

GOLDEN = Path(__file__).parent / "golden" / "html_md"
CASES = sorted(path.stem for path in GOLDEN.glob("*.html"))


@pytest.mark.parametrize("name", CASES)
def test_golden_file_renders_byte_for_byte(name: str) -> None:
    html = (GOLDEN / f"{name}.html").read_text(encoding="utf-8")
    expected = (GOLDEN / f"{name}.md").read_text(encoding="utf-8")
    renderer = HtmlMarkdownRenderer()
    assert renderer.render(html) == expected
    assert renderer.render(html) == expected  # no state leaks between renders


def test_golden_cases_cover_the_planned_inputs() -> None:
    assert {
        "headings_paragraphs",
        "nested_lists",
        "table_pipes_newlines",
        "nested_table",
        "code_language",
        "quote_links_images",
        "unclosed_tags",
        "entities_whitespace",
        "empty",
    } <= set(CASES)


def test_handlers_extend_and_override_rendering() -> None:
    def macro(node: HtmlNode, ctx: RenderContext) -> Block:
        return Block(f"> **{node.get('ac:name')}**: {ctx.inline(node)}")

    def mention(node: HtmlNode, ctx: RenderContext) -> str:
        return f"@{node.get('data-name')}"

    renderer = HtmlMarkdownRenderer({"ac:structured-macro": macro, "span": mention})
    html = '<p>Ask <span data-name="Ana">x</span></p><ac:structured-macro ac:name="note">Read me</ac:structured-macro>'
    assert renderer.render(html) == "Ask @Ana\n\n> **note**: Read me\n"


def test_cdata_becomes_text_and_deep_nesting_does_not_recurse_forever() -> None:
    root = parse_html("<x><![CDATA[a < b && c]]></x>")
    assert root.text() == "a < b && c"
    deep = "<div>" * 5000 + "bottom" + "</div>" * 5000
    assert HtmlMarkdownRenderer().render(deep) == "bottom\n"


def test_every_line_is_stripped_and_output_ends_with_one_newline() -> None:
    out = HtmlMarkdownRenderer().render("<p>  a  </p>\n\n\n<p>\tb\t</p>")
    assert out == "a\n\nb\n"
