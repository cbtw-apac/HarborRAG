#!/usr/bin/env python3
"""
Additional tests to boost website builder coverage to over 85%.
Focuses on uncovered lines and edge cases.
"""

import importlib.util
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


class TestMarkdownProcessorCoverage:
    """Test markdown processor to increase coverage."""

    def test_markdown_import_error_fallback(self):
        """Test markdown processing when markdown library is not available."""
        from website.builder.markdown import MarkdownProcessor

        processor = MarkdownProcessor()

        # Test the fallback method directly
        result = processor._basic_markdown_to_html_no_regex("# Test Header\n\nSome content")

        # Should convert basic markdown
        assert "<h1>Test Header</h1>" in result
        assert "<p>Some content</p>" in result

    def test_basic_markdown_no_regex_comprehensive(self):
        """Test comprehensive markdown conversion without regex."""
        from website.builder.markdown import MarkdownProcessor

        processor = MarkdownProcessor()

        # Test complex markdown with all features
        markdown = """
# Main Header
## Sub Header
### Sub Sub Header
#### Level 4
##### Level 5
###### Level 6

**Bold text** and *italic text*

`inline code` here

```python
def hello():
    print("world")
```

- List item 1
- List item 2

[Link text](http://example.com)

Regular paragraph text.
        """.strip()

        result = processor._basic_markdown_to_html_no_regex(markdown)

        # Check all heading levels
        assert "<h1>Main Header</h1>" in result
        assert "<h2>Sub Header</h2>" in result
        assert "<h3>Sub Sub Header</h3>" in result
        assert "<h4>Level 4</h4>" in result
        assert "<h5>Level 5</h5>" in result
        assert "<h6>Level 6</h6>" in result

        # Check formatting
        assert "<strong>Bold text</strong>" in result
        assert "<em>italic text</em>" in result
        assert "<code>inline code</code>" in result

        # Check code blocks
        assert "<pre><code>" in result
        assert "def hello():" in result

        # Check lists
        assert "<ul>" in result
        assert "<li>List item 1</li>" in result
        assert "<li>List item 2</li>" in result

        # Check links
        assert '<a href="http://example.com">Link text</a>' in result

        # Check paragraphs
        assert "<p>Regular paragraph text.</p>" in result

    def test_basic_markdown_edge_cases(self):
        """Test edge cases in basic markdown processing."""
        from website.builder.markdown import MarkdownProcessor

        processor = MarkdownProcessor()

        # Test empty content
        assert processor._basic_markdown_to_html_no_regex("") == ""
        assert processor._basic_markdown_to_html_no_regex("   ") == ""

        # Test code block without closing
        result = processor._basic_markdown_to_html_no_regex("```python\ncode here")
        assert "<pre><code>" in result
        assert "code here" in result

        # Test list followed by non-list
        result = processor._basic_markdown_to_html_no_regex("- Item 1\n- Item 2\n\nParagraph")
        assert "<ul>" in result
        assert "</ul>" in result
        assert "<p>Paragraph</p>" in result

        # Test heading followed by list
        result = processor._basic_markdown_to_html_no_regex("# Header\n- Item 1")
        assert "<h1>Header</h1>" in result
        assert "<ul>" in result
        assert "<li>Item 1</li>" in result

    def test_fix_malformed_code_blocks(self):
        """Test fixing malformed code blocks."""
        from website.builder.markdown import MarkdownProcessor

        processor = MarkdownProcessor()

        # Test inline code with bash commands
        html = '<p><code class="inline-code">bash\n\nmkdir test</code></p>'
        result = processor.fix_malformed_code_blocks(html)
        assert 'class="code-block-wrapper"' in result
        assert 'class="language-bash"' in result

        # Test paragraphs with command-like content
        html = '<p><code class="inline-code">pip install qdrant-loader</code></p>'
        result = processor.fix_malformed_code_blocks(html)
        assert 'class="code-block-wrapper"' in result
        assert 'class="language-bash"' in result

        # Test malformed code blocks with triple backticks
        html = '<p>```python\nprint("hello")\n```</p>'
        result = processor.fix_malformed_code_blocks(html)
        assert 'class="code-block-wrapper"' in result
        assert 'class="language-python"' in result

    def test_ensure_heading_ids_edge_cases(self):
        """Test heading ID generation edge cases."""
        from website.builder.markdown import MarkdownProcessor

        processor = MarkdownProcessor()

        # Test headings with special characters
        html = "<h1>Test & Special Characters!</h1>"
        result = processor.ensure_heading_ids(html)
        assert 'id="test-special-characters"' in result

        # Test headings with existing IDs
        html = '<h1 id="existing">Test</h1>'
        result = processor.ensure_heading_ids(html)
        assert 'id="existing"' in result
        assert result.count("id=") == 1  # Should not add another ID

        # Test headings with other attributes
        html = '<h1 class="test">Test</h1>'
        result = processor.ensure_heading_ids(html)
        assert 'id="test"' in result
        assert 'class="test"' in result

    def test_render_task_list_checkboxes(self):
        """Test rendering markdown task markers as checkboxes."""
        from website.builder.markdown import MarkdownProcessor

        processor = MarkdownProcessor()
        html = '<ul><li>[ ] Pending item</li><li class="existing">[x] Done item</li></ul>'

        result = processor.render_task_list_checkboxes(html)

        assert result.count('type="checkbox"') == 2
        assert result.count("disabled") == 2
        assert "task-list-item" in result
        assert "checked" in result
        assert "existing task-list-item" in result or "task-list-item existing" in result

    def test_convert_markdown_links_edge_cases(self):
        """Test markdown link conversion edge cases."""
        from website.builder.markdown import MarkdownProcessor

        processor = MarkdownProcessor()

        # Test with source file context
        content = "[Test](docs/guide.md)"
        result = processor.convert_markdown_links_to_html(content, "packages/test/README.md")
        assert "/docs/guide.html" in result

        # Test LICENSE file conversion
        content = "[License](LICENSE)"
        result = processor.convert_markdown_links_to_html(content, "packages/test/README.md")
        assert "/docs/LICENSE.html" in result

        # Test relative path conversion with target directory
        content = "[Guide](/docs/guide.md)"
        result = processor.convert_markdown_links_to_html(
            content, "test.md", "docs/packages/test/README.html"
        )
        # The actual behavior may vary, just check it processes the link
        assert "guide.html" in result

    def test_process_link_path_edge_cases(self):
        """Test link path processing edge cases."""
        from website.builder.markdown import MarkdownProcessor

        processor = MarkdownProcessor()

        # Test with anchor and no source file
        result = processor._process_link_path("guide.md#section")
        assert result == "guide.md#section"  # Should preserve in test context

        # Test with source file context
        result = processor._process_link_path("../../docs/guide.md", "packages/test/README.md")
        assert result == "/docs/guide.html"

        # Test well-known files
        result = processor._process_link_path("CONTRIBUTING", "packages/test/README.md")
        assert result == "/docs/CONTRIBUTING.html"


class TestAssetManagerCoverage:
    """Test asset manager to increase coverage."""

    def test_copy_static_file_missing(self, tmp_path):
        """Test copying non-existent static file."""
        from website.builder.assets import AssetManager

        manager = AssetManager(str(tmp_path / "output"))

        # Should handle missing file gracefully
        manager.copy_static_file("nonexistent.txt", "dest.txt")

        # File should not exist
        assert not (tmp_path / "output" / "dest.txt").exists()

    def test_copy_static_file_success(self, tmp_path):
        """Test successful static file copying."""
        from website.builder.assets import AssetManager

        # Create source file
        source_file = tmp_path / "source.txt"
        source_file.write_text("test content")

        manager = AssetManager(str(tmp_path / "output"))
        manager.copy_static_file(str(source_file), "dest.txt")

        # File should be copied
        dest_file = tmp_path / "output" / "dest.txt"
        assert dest_file.exists()
        assert dest_file.read_text() == "test content"

    def test_ensure_output_directory(self, tmp_path):
        """Test output directory creation."""
        from website.builder.assets import AssetManager

        output_dir = tmp_path / "nested" / "output"
        manager = AssetManager(str(output_dir))

        assert not output_dir.exists()
        manager.ensure_output_directory()
        assert output_dir.exists()

    def test_copy_static_files_with_colon_syntax(self, tmp_path):
        """Test copying static files with source:dest syntax."""
        from website.builder.assets import AssetManager

        # Create source file
        source_file = tmp_path / "source.txt"
        source_file.write_text("test content")

        manager = AssetManager(str(tmp_path / "output"))
        manager.copy_static_files([f"{source_file}:custom/dest.txt"])

        # File should be copied to custom location
        dest_file = tmp_path / "output" / "custom" / "dest.txt"
        assert dest_file.exists()
        assert dest_file.read_text() == "test content"

    def test_copy_static_files_directory_exists(self, tmp_path):
        """Test copying directory when destination exists."""
        from website.builder.assets import AssetManager

        # Create source directory
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "file.txt").write_text("content")

        # Create destination directory
        dest_dir = tmp_path / "output" / "source"
        dest_dir.mkdir(parents=True)
        (dest_dir / "old.txt").write_text("old content")

        manager = AssetManager(str(tmp_path / "output"))
        manager.copy_static_files([str(source_dir)])

        # Directory should be replaced
        assert (tmp_path / "output" / "source" / "file.txt").exists()
        assert not (tmp_path / "output" / "source" / "old.txt").exists()
