from __future__ import annotations

from pathlib import Path

import local
import pytest

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _parsed(content: str, *, links: list[dict] | None = None) -> ParsedDocument:
    metadata = {"links": links} if links else {}
    return ParsedDocument(
        content=content,
        parser_name="html",
        parser_version="1.0.0",
        elements=[
            DocumentElement(id="html:0", type="paragraph", content=content, metadata=metadata)
        ],
    )


def test_rendered_markdown_includes_a_links_section_for_captured_anchors() -> None:
    parsed = _parsed(
        "Visit\nour pricing page\nfor details.",
        links=[
            {
                "href": "https://example.com/pricing",
                "title": "Pricing Page",
                "text": "our pricing page",
            }
        ],
    )

    output = local._render_local_output(Path("page.html"), parsed, markdown=True)

    assert "## Links" in output
    assert "- [our pricing page](https://example.com/pricing) — Pricing Page" in output


def test_rendered_markdown_omits_links_section_when_no_anchors_present() -> None:
    parsed = _parsed("No links here.")

    output = local._render_local_output(Path("page.html"), parsed, markdown=True)

    assert "## Links" not in output
