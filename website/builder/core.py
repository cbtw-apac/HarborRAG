"""
Core Website Builder - Main Orchestration and Lifecycle Management.

This module implements the main WebsiteBuilder class that orchestrates
all build operations and manages the overall build lifecycle.
"""

import subprocess
from pathlib import Path

from .assets import AssetManager
from .coverage import CoverageBuildMixin
from .markdown import MarkdownProcessor
from .package_docs import PackageDocsMixin
from .pages import PageBuildMixin
from .project import ProjectStructureMixin
from .seo import SeoBuildMixin
from .site import SiteBuildMixin
from .templates import TemplateProcessor


class WebsiteBuilder(
    ProjectStructureMixin,
    PageBuildMixin,
    SiteBuildMixin,
    SeoBuildMixin,
    CoverageBuildMixin,
    PackageDocsMixin,
):
    """Builds the HarborRAG documentation website from repository sources."""

    def __init__(self, templates_dir: str = "website/templates", output_dir: str = "site"):
        """Initialize the website builder."""
        self.templates_dir = Path(templates_dir)
        self.output_dir = Path(output_dir)
        self.base_url = ""
        # Cached docs navigation data (built once per run)
        self.docs_nav_data: dict | None = None

        # Initialize component processors
        self.template_processor = TemplateProcessor(templates_dir)
        self.markdown_processor = MarkdownProcessor()
        self.markdown_processor.repository_url = "https://github.com/cbtw-apac/HarborRAG"
        self.asset_manager = AssetManager(output_dir)

    # Delegate core operations to specialized processors
    def load_template(self, template_name: str) -> str:
        """Load a template file."""
        return self.template_processor.load_template(template_name)

    def replace_placeholders(self, content: str, replacements: dict[str, str]) -> str:
        """Replace placeholders in content with actual values."""
        return self.template_processor.replace_placeholders(content, replacements)

    def markdown_to_html(
        self, markdown_content: str, source_file: str = "", output_file: str = ""
    ) -> str:
        """Convert markdown to HTML with Bootstrap styling."""
        return self.markdown_processor.markdown_to_html(markdown_content, source_file, output_file)

    def copy_assets(self) -> None:
        """Copy all website assets to output directory."""
        return self.asset_manager.copy_assets()

    def extract_title_from_markdown(self, markdown_content: str) -> str:
        """Extract title from markdown content."""
        return self.markdown_processor.extract_title_from_markdown(markdown_content)

    # Additional markdown processing methods
    def basic_markdown_to_html(self, markdown_content: str) -> str:
        """Basic markdown to HTML conversion."""
        return self.markdown_processor.basic_markdown_to_html(markdown_content)

    def convert_markdown_links_to_html(
        self, markdown_content: str, source_file: str = "", target_dir: str = ""
    ) -> str:
        """Convert markdown links to HTML format."""
        return self.markdown_processor.convert_markdown_links_to_html(
            markdown_content, source_file, target_dir
        )

    def add_bootstrap_classes(self, html_content: str) -> str:
        """Add Bootstrap classes to HTML elements."""
        return self.markdown_processor.add_bootstrap_classes(html_content)

    def render_toc(self, html_content: str) -> str:
        """Generate table of contents from HTML headings."""
        return self.markdown_processor.render_toc(html_content)

    # Additional asset management methods
    def copy_static_files(self, static_files: list[str]) -> None:
        """Copy multiple static files."""
        return self.asset_manager.copy_static_files(static_files)

    def get_git_timestamp(self, source_path: str) -> str:
        """Get the last modified timestamp from Git."""
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%cd", "--date=iso-strict", source_path],
                capture_output=True,
                text=True,
                cwd=".",
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return ""

    def _humanize_title(self, name: str) -> str:
        """Convert filename to human-readable title."""
        # Remove file extension and common prefixes
        title = name.replace(".md", "").replace("README", "").replace("_", " ").replace("-", " ")

        # Handle common patterns
        title_mappings = {
            "cli reference": "CLI Reference",
            "api": "API",
            "faq": "FAQ",
            "toc": "Table of Contents",
            "readme": "Overview",
        }

        title_lower = title.lower().strip()
        if title_lower in title_mappings:
            return title_mappings[title_lower]

        # Capitalize words
        return " ".join(word.capitalize() for word in title.split())
