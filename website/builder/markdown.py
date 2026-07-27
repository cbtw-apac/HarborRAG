"""
Markdown Processing - Markdown-to-HTML Conversion.

This module handles markdown processing, HTML conversion,
and content formatting for the website builder.
"""

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
            # Fallback to basic conversion
            html = self._basic_markdown_to_html_no_regex(markdown_content)
            # Apply Bootstrap classes to fallback HTML too
            html = self.add_bootstrap_classes(html)
            # Render task lists in fallback mode too
            html = self.render_task_list_checkboxes(html)
            # Ensure heading IDs
            html = self.ensure_heading_ids(html)
            return html
