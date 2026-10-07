"""Readable Markdown rendering for Confluence's server-expanded HTML preview."""

from __future__ import annotations

from harborrag_adapters.parsers.common.html_markdown import html_to_markdown

# Chrome the rendered view adds around real content: status icons and the
# expand/collapse arrows on macros. They carry no text worth keeping and
# would otherwise leave stray bullets or empty inline elements behind.
_CONFLUENCE_CHROME_SELECTORS = (".aui-icon", ".expand-control-image")


def confluence_html_to_markdown(value: str) -> str:
    """Convert rendered Confluence HTML to deterministic, readable Markdown."""
    return html_to_markdown(value, drop_selectors=_CONFLUENCE_CHROME_SELECTORS)
