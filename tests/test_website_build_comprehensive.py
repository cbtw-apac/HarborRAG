#!/usr/bin/env python3
"""
Comprehensive tests for the website build system to achieve >90% coverage.
Tests all aspects of the GitHub Actions docs workflow.
"""

import importlib.util
import os
from pathlib import Path

import pytest


def import_website_builder():
    """Import WebsiteBuilder class dynamically to avoid linter issues."""
    website_dir = Path(__file__).parent.parent / "website"
    build_file = website_dir / "build.py"

    if not build_file.exists():
        pytest.skip("Website build.py not found", allow_module_level=True)

    spec = importlib.util.spec_from_file_location("build", build_file)
    if spec is None or spec.loader is None:
        pytest.skip("Cannot load build module", allow_module_level=True)

    build_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_module)

    return build_module.WebsiteBuilder, build_module.main


# Import the classes at module level
try:
    WebsiteBuilder, main_function = import_website_builder()
except Exception:
    pytest.skip("Cannot import WebsiteBuilder", allow_module_level=True)


class TestWebsiteBuilderCore:
    """Test core WebsiteBuilder functionality."""

    def test_websitebuilder_init(self):
        """Test WebsiteBuilder initialization."""
        builder = WebsiteBuilder()
        assert builder.templates_dir == Path("website/templates")
        assert builder.output_dir == Path("site")
        assert builder.base_url == ""

        # Test custom paths
        builder = WebsiteBuilder("custom/templates", "custom/output")
        assert builder.templates_dir == Path("custom/templates")
        assert builder.output_dir == Path("custom/output")

    def test_load_template_success(self, mock_project_structure):
        """Test successful template loading."""
        os.chdir(mock_project_structure)
        builder = WebsiteBuilder("website/templates", "site")

        content = builder.load_template("base.html")
        assert "{{ page_title }}" in content
        assert "{{ content }}" in content

    def test_load_template_not_found(self, mock_project_structure):
        """Test template loading with missing file."""
        os.chdir(mock_project_structure)
        builder = WebsiteBuilder("website/templates", "site")

        with pytest.raises(FileNotFoundError):
            builder.load_template("nonexistent.html")

    def test_replace_placeholders(self):
        """Test placeholder replacement."""
        builder = WebsiteBuilder()
        content = "Hello {{ name }}, welcome to {{ site }}!"
        replacements = {"name": "John", "site": "QDrant Loader"}

        result = builder.replace_placeholders(content, replacements)
        assert result == "Hello John, welcome to QDrant Loader!"

    def test_replace_placeholders_empty(self):
        """Test placeholder replacement with empty replacements."""
        builder = WebsiteBuilder()
        content = "Hello {{ name }}!"

        result = builder.replace_placeholders(content, {})
        assert result == "Hello {{ name }}!"

    def test_extract_title_from_markdown(self):
        """Test title extraction from markdown."""
        builder = WebsiteBuilder()

        # Test with h1
        markdown = "# Main Title\n\nSome content"
        title = builder.extract_title_from_markdown(markdown)
        assert title == "Main Title"

        # Test with no title
        markdown = "Just some content"
        title = builder.extract_title_from_markdown(markdown)
        assert title == "Documentation"

        # Test with multiple headers
        markdown = "# First Title\n## Second Title"
        title = builder.extract_title_from_markdown(markdown)
        assert title == "First Title"


class TestWebsiteBuilderMarkdown:
    """Test markdown processing functionality."""

    def test_basic_markdown_to_html_headers(self):
        """Test basic markdown header conversion."""
        builder = WebsiteBuilder()

        markdown = "# Header 1\n## Header 2\n### Header 3\n#### Header 4"
        html = builder.basic_markdown_to_html(markdown)

        assert 'class="display-4 fw-bold text-primary mb-4"' in html
        assert 'class="h2 fw-bold text-primary"' in html
        assert 'class="h3 fw-bold text-primary"' in html
        assert 'class="h4 fw-bold"' in html

    def test_basic_markdown_to_html_code(self):
        """Test basic markdown code conversion."""
        builder = WebsiteBuilder()

        # Test code blocks
        markdown = "```python\nprint('hello')\n```"
        html = builder.basic_markdown_to_html(markdown)
        # The output varies depending on whether the markdown library is available
        # Just verify it contains code-related elements
        assert "<code" in html
        assert "print('hello')" in html or "print(&#39;hello&#39;)" in html

        # Test inline code
        markdown = "Use `pip install` to install"
        html = builder.basic_markdown_to_html(markdown)
        assert 'class="inline-code"' in html

    def test_basic_markdown_to_html_links(self):
        """Test basic markdown link conversion."""
        builder = WebsiteBuilder()

        markdown = "[QDrant Loader](https://github.com/user/repo)"
        html = builder.basic_markdown_to_html(markdown)
        assert 'class="text-decoration-none"' in html
        assert 'href="https://github.com/user/repo"' in html

    def test_basic_markdown_to_html_formatting(self):
        """Test basic markdown formatting conversion."""
        builder = WebsiteBuilder()

        markdown = "**bold text** and *italic text*"
        html = builder.basic_markdown_to_html(markdown)
        assert "<strong>bold text</strong>" in html
        assert "<em>italic text</em>" in html

    def test_basic_markdown_to_html_lists(self):
        """Test basic markdown list conversion."""
        builder = WebsiteBuilder()

        markdown = "- Item 1\n- Item 2"
        html = builder.basic_markdown_to_html(markdown)
        assert "Item 1" in html
        assert "Item 2" in html
        assert 'class="list-group list-group-flush"' in html
        assert 'class="list-group-item"' in html

    def test_convert_markdown_links_to_html(self):
        """Test markdown link to HTML conversion."""
        builder = WebsiteBuilder()

        # Test relative links
        html = 'href="./docs/guide.md"'
        result = builder.convert_markdown_links_to_html(html)
        assert 'href="./docs/guide.html"' in result

        # Test absolute links
        html = 'href="/docs/api.md"'
        result = builder.convert_markdown_links_to_html(html)
        assert 'href="/docs/api.html"' in result

        # Test without ./ prefix
        html = 'href="guide.md"'
        result = builder.convert_markdown_links_to_html(html)
        assert 'href="guide.html"' in result

    def test_add_bootstrap_classes(self):
        """Test Bootstrap class addition."""
        builder = WebsiteBuilder()

        html = "<h1>Title</h1><h2>Subtitle</h2><h3>Section</h3><h4>Subsection</h4>"
        result = builder.add_bootstrap_classes(html)

        assert 'class="display-4 fw-bold text-primary mb-4"' in result
        assert 'class="h2 fw-bold text-primary"' in result
        assert 'class="h3 fw-bold text-primary"' in result
        assert 'class="h4 fw-bold"' in result

    def test_add_bootstrap_classes_numbered_step_paragraphs(self):
        """Regression: numbered step paragraphs should keep list-group styling."""
        builder = WebsiteBuilder()

        html = (
            "<p>1. <strong>Fork and Clone</strong></p>"
            '<div class="code-block-wrapper"><pre class="code-block"><code>cmd</code></pre></div>'
            "<p>2. <strong>Install Dependencies</strong></p>"
        )
        result = builder.add_bootstrap_classes(html)

        assert '<ol start="1" class="list-group list-group-numbered">' in result
        assert '<ol start="2" class="list-group list-group-numbered">' in result
        assert '<li class="list-group-item"><strong>Fork and Clone</strong></li>' in result
        assert '<li class="list-group-item"><strong>Install Dependencies</strong></li>' in result

    def test_add_bootstrap_classes_wraps_tables_for_scrolling(self):
        """Wide tables scroll through a wrapper without changing table semantics."""
        builder = WebsiteBuilder()

        result = builder.add_bootstrap_classes("<table><tr><td>Cell</td></tr></table>")

        assert result.startswith('<div class="table-scroll"')
        assert '<table class="table table-striped table-hover">' in result
        assert result.endswith("</table></div>")

    def test_markdown_to_html_with_markdown_library(self):
        """Test markdown conversion with markdown library available."""
        builder = WebsiteBuilder()

        # Test with simple markdown - this will use whatever markdown processing is available
        result = builder.markdown_to_html("# Test Header")

        # Should contain some HTML output
        assert len(result) > 0
        assert "Test Header" in result
        # Should have some HTML tags or Bootstrap classes
        assert ("<" in result and ">" in result) or "class=" in result

    def test_markdown_to_html_fallback(self):
        """Test markdown conversion fallback when library unavailable."""
        # Test the fallback method directly to avoid mocking issues
        builder = WebsiteBuilder()
        result = builder.markdown_processor._basic_markdown_to_html_no_regex("# Test Header")

        # Should convert basic markdown
        assert "<h1>Test Header</h1>" in result

    def test_markdown_to_html_renders_task_list_checkboxes(self):
        """Task list markers should render as disabled checkbox inputs."""
        builder = WebsiteBuilder()

        markdown = "- [ ] Pending item\n- [x] Done item"
        result = builder.markdown_to_html(markdown)

        assert 'type="checkbox"' in result
        assert "disabled" in result
        assert "task-list-item" in result
        assert "checked" in result
        assert "Pending item" in result
        assert "Done item" in result
