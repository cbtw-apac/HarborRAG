"""
Markdown Processing - Markdown-to-HTML Conversion.

This module handles markdown processing, HTML conversion,
and content formatting for the website builder.
"""

import sys

from .markdown_fallback import MarkdownFallbackMixin
from .markdown_html import MarkdownHtmlMixin
from .markdown_links import MarkdownLinksMixin
from .markdown_toc import MarkdownTocMixin


class MarkdownProcessor(
    MarkdownFallbackMixin,
    MarkdownHtmlMixin,
    MarkdownLinksMixin,
    MarkdownTocMixin,
):
    """Handles markdown processing and HTML conversion."""

    def markdown_to_html(
        self, markdown_content: str, source_file: str = "", output_file: str = ""
    ) -> str:
        """Convert markdown to HTML with Bootstrap styling."""
        # Normalize empty/whitespace-only content consistently across code paths
        if not markdown_content.strip():
            return ""
        try:
            import markdown

            md = markdown.Markdown(
                extensions=[
                    # Supports fenced code blocks reliably inside list items (superset of fenced_code).
                    "pymdownx.superfences",
                    "codehilite",
                    "tables",
                    "toc",
                    "attr_list",
                    "def_list",
                    "footnotes",
                    "md_in_html",
                    "sane_lists",
                ],
                extension_configs={
                    "pymdownx.superfences": {
                        "custom_fences": []  # Disable custom fences that might use Pygments
                    },
                    "codehilite": {
                        "css_class": "codehilite",
                        "use_pygments": False,  # Use simple highlighting without Pygments
                        "guess_lang": True,
                    },
                },
            )
            html = md.convert(markdown_content)

            # Fix any remaining malformed code blocks
            html = self.fix_malformed_code_blocks(html)

            # Add Bootstrap classes
            html = self.add_bootstrap_classes(html)

            # Render GitHub-style task list markers as clickable checkboxes
            html = self.render_task_list_checkboxes(html)

            # Ensure heading IDs
            html = self.ensure_heading_ids(html)

            return html

        except ImportError:
            # The `markdown` package lives in the `docs` extra, so a plain
            # `uv sync --extra dev` leaves it missing and every page silently
            # renders through the degraded fallback: tables stay raw `|` pipes,
            # syntax highlighting and heading anchors are lost, and the build
            # still reports success. Say so loudly instead.
            _warn_degraded_renderer()
            # Fallback to basic conversion
            html = self._basic_markdown_to_html_no_regex(markdown_content)
            # Apply Bootstrap classes to fallback HTML too
            html = self.add_bootstrap_classes(html)
            # Render task lists in fallback mode too
            html = self.render_task_list_checkboxes(html)
            # Ensure heading IDs
            html = self.ensure_heading_ids(html)
            return html


_DEGRADED_RENDERER_WARNED = False


def _warn_degraded_renderer() -> None:
    """Warn once per process that pages are rendering without `markdown`."""

    global _DEGRADED_RENDERER_WARNED
    if _DEGRADED_RENDERER_WARNED:
        return
    _DEGRADED_RENDERER_WARNED = True
    print(
        "\n"
        "WARNING: the `markdown` package is not importable, so the website is\n"
        "         being built with the degraded fallback renderer. Markdown\n"
        "         tables will render as raw `|` pipes and the build will still\n"
        "         report success.\n"
        "         Fix with one of:\n"
        "           uv sync --all-packages --all-extras\n"
        "           uv run --with markdown --with pygments --with pymdown-extensions \\\n"
        "                  --with tomli python website/build.py --output site\n",
        file=sys.stderr,
    )
