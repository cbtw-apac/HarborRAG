"""PackageDocsMixin implementation for the website builder."""

import re
from pathlib import Path


class PackageDocsMixin:
    """Focused website-build operations composed by ``WebsiteBuilder``."""

    def build_package_docs(self) -> None:
        """Build documentation pages from package README files into docs/packages.

        Maps package README.md files to site docs under:
          - packages/qdrant-loader -> docs/packages/qdrant-loader/README.html
          - packages/qdrant-loader-mcp-server -> docs/packages/mcp-server/README.html
          - packages/qdrant-loader-core -> docs/packages/core/README.html
        """
        package_mappings: list[tuple[str, str, str]] = [
            ("qdrant-loader", "qdrant-loader", "QDrant Loader"),
            ("qdrant-loader-mcp-server", "mcp-server", "MCP Server"),
            ("qdrant-loader-core", "core", "Core Library"),
        ]

        for pkg_name, alias, display_name in package_mappings:
            readme_path = Path("packages") / pkg_name / "README.md"
            if not readme_path.exists():
                continue

            try:
                with open(readme_path, encoding="utf-8") as f:
                    markdown_content = f.read()

                # Normalize links in markdown before conversion
                normalized_md = self.markdown_processor.convert_markdown_links_to_html(
                    markdown_content
                )

                html_content = self.markdown_to_html(
                    normalized_md,
                    str(readme_path),
                    f"docs/packages/{alias}/README.html",
                )
                # Normalize any remaining HTML hrefs
                html_content = self.markdown_processor.convert_markdown_links_to_html(
                    html_content, str(readme_path), f"docs/packages/{alias}/README.html"
                )

                # Final hardening for package README links: collapse relative ../../docs to /docs
                try:
                    html_content = re.sub(r'href="(?:\.{2}/)+docs/', 'href="/docs/', html_content)
                    # Convert README root files and .md links under docs to .html
                    html_content = re.sub(
                        r'href="(?:\.{2}/)+CONTRIBUTING\.md"',
                        'href="/docs/CONTRIBUTING.html"',
                        html_content,
                    )
                    html_content = re.sub(
                        r'href="(?:\.{2}/)+LICENSE(\.html)?"',
                        'href="/docs/LICENSE.html"',
                        html_content,
                    )
                    html_content = re.sub(
                        r'href="(?:\.{2}/)+docs/([^"#]+)\.md(#[^"]*)?"',
                        r'href="/docs/\1.html\2"',
                        html_content,
                    )
                except Exception:
                    pass

                # Build a Table of Contents and wrap with standard docs layout for consistent look
                toc_html = self.render_toc(html_content)
                if toc_html:
                    toc_html = self.add_bootstrap_classes(toc_html)

                wrapped_content = f"""
<section>
   <div class=\"container-fluid\">
    <div class=\"row toc-layout\">
      <aside class=\"toc-sidebar d-none d-lg-block p-0\">
        <div class=\"position-sticky\">
          {toc_html or '<div class="text-muted small">No sections</div>'}
        </div>
      </aside>
      <div class=\"container-content\">
        {html_content}
      </div>
    </div>
    </div>
</section>
"""

                output_path = f"docs/packages/{alias}/README.html"
                self.build_page(
                    "base.html",
                    output_path,
                    f"{display_name} - README",
                    f"{display_name} Documentation",
                    output_path,
                    content=wrapped_content,
                )
            except Exception as e:
                print(f"⚠️  Failed to build docs for package {pkg_name}: {e}")

    def generate_directory_indexes(self) -> None:
        """Generate index files for directories."""
        # Look in both source docs and output site docs directories
        source_docs_dir = Path("docs")
        site_docs_dir = self.output_dir / "docs"

        # Process directories in both locations
        for docs_dir in [source_docs_dir, site_docs_dir]:
            if not docs_dir.exists():
                continue

            for directory in docs_dir.rglob("*"):
                if directory.is_dir():
                    # Look for README or index files in various formats
                    readme_md = directory / "README.md"
                    readme_html = directory / "README.html"
                    index_md = directory / "index.md"
                    index_html = directory / "index.html"

                    # Determine source file
                    source_file = None
                    if readme_md.exists():
                        source_file = readme_md
                    elif index_md.exists():
                        source_file = index_md
                    elif readme_html.exists():
                        source_file = readme_html
                    elif index_html.exists():
                        source_file = index_html

                    if source_file:
                        try:
                            if docs_dir == site_docs_dir:
                                # For files in site directory, create/overwrite index.html directly there
                                index_file = directory / "index.html"
                                if source_file.suffix == ".html":
                                    # Copy HTML file content directly (always overwrite to avoid stale links)
                                    content = source_file.read_text(encoding="utf-8")
                                    index_file.write_text(content, encoding="utf-8")
                                    print(f"📄 Generated index.html from {source_file.name}")
                            else:
                                # For source files, process through normal build pipeline
                                relative_dir = directory.relative_to(docs_dir)
                                output_path = f"docs/{relative_dir}/index.html"

                                if source_file.suffix == ".html":
                                    # Copy HTML file content directly
                                    content = source_file.read_text(encoding="utf-8")
                                    self.build_page(
                                        "base.html",
                                        output_path,
                                        self._humanize_title(directory.name),
                                        f"{self._humanize_title(directory.name)} Documentation",
                                        output_path,
                                        content=content,
                                    )
                                else:
                                    # Process markdown file
                                    self.build_markdown_page(
                                        str(source_file),
                                        output_path,
                                        title=self._humanize_title(directory.name),
                                    )
                        except Exception as e:
                            print(f"⚠️  Failed to generate index for {directory}: {e}")
