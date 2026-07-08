from __future__ import annotations

from typing import ClassVar

from harborrag_adapters.parsers.markdown import MarkdownParser


class MockMarkdownParser(MarkdownParser):
    """Backward-compatible Markdown parser name used by tests and examples."""

    parser_name: ClassVar[str] = "mock_markdown"
