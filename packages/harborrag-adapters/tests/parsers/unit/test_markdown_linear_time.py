"""Markdown text extraction must stay linear on hostile markup.

Inputs up to 128 MiB are accepted and any connector that delivers markdown
reaches this parser, so a quadratic substitution wedges a worker.
"""

from __future__ import annotations

import re
import time

import pytest

from harborrag_adapters.parsers.markup.engines.markdown.engine import MarkdownParser
from harborrag_core.domain.parser import ParseInput

# Each input took several seconds with the backtracking patterns (measured:
# unclosed links ~6 s, image openers ~6 s, blank lines ~12 s). The bound is
# generous so a loaded machine cannot trip it.
ADVERSARIAL_MARKDOWN = {
    "unclosed link targets": "[a](" * 80_000,
    "empty-label link openers": "[](" * 80_000,
    "unclosed image labels": "![" * 40_000,
    "blank lines": "\n" * 40_000,
    "whitespace-only lines": " \n" * 40_000,
}


@pytest.mark.parametrize("content", ADVERSARIAL_MARKDOWN.values(), ids=ADVERSARIAL_MARKDOWN.keys())
def test_markdown_parse_is_linear_on_hostile_markup(content: str) -> None:
    started = time.perf_counter()
    document = MarkdownParser().parse(ParseInput(content=content, filename="hostile.md"))
    elapsed = time.perf_counter() - started

    assert document.parser_name == "markdown"
    assert elapsed < 5.0, f"markdown parse took {elapsed:.2f}s"


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("See [Harbor](https://example.com) and ![logo](logo.png).", "See Harbor and logo."),
        ("- first\n* second\n+ third", "first\nsecond\nthird"),
        ("1. one\n2) two", "one\ntwo"),
        ("  - indented", "indented"),
        ("\t- tabbed", "tabbed"),
        ("# Title\n\n> quoted *bold*", "Title\nquoted bold"),
    ],
)
def test_markdown_text_strips_common_markup(markdown: str, expected: str) -> None:
    document = MarkdownParser().parse(ParseInput(content=markdown, filename="doc.md"))

    assert document.content == expected


def _previous_to_text(markdown: str) -> str:
    """Copy of the backtracking implementation, kept to pin ordinary output."""

    text = markdown
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`~]", "", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


ORDINARY_DOCUMENTS = [
    "# Guide\n\nSee [Harbor](https://example.com/a?b=c) and ![logo](img/logo.png).\n",
    "- one\n- two [x](y.md)\n  - nested\n1. first\n2) second\n10. tenth\n",
    "> quote with `code` and ~~strike~~\n> second line\n\n## Sub *heading*\n",
    "Text\n\n\n\nMore text with [a](b) [c](d) and ![](empty-alt.png)\n",
    "\t- tabbed\n   * spaced\n+ plus\n3) three\n",
    "[link]: reference style stays\n[text][ref] and [plain] brackets (parens)\n",
    "",
]


@pytest.mark.parametrize("markdown", ORDINARY_DOCUMENTS)
def test_markdown_to_text_matches_previous_output_on_ordinary_documents(markdown: str) -> None:
    assert MarkdownParser._to_text(markdown) == _previous_to_text(markdown)


def test_markdown_blank_line_before_list_is_kept() -> None:
    # The old "^\\s*" marker patterns crossed newlines and swallowed the blank
    # line between a paragraph and a following list; markers now stay on their line.
    assert MarkdownParser._to_text("para\n\n- a\n- b") == "para\n\na\nb"
    assert MarkdownParser._to_text("[x](y(1).md)") == "[x](y(1).md)"
