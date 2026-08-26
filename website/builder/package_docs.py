"""Package README discovery and rendering for the documentation website."""

import html
import re
from pathlib import Path


class PackageDocsMixin:
    """Focused website-build operations composed by ``WebsiteBuilder``."""

    def build_package_docs(self) -> None:
        """Discover packages from their metadata and render every package README."""
        for package in self.discover_package_docs():
            pkg_name = package["directory"]
            display_name = package["name"]
            readme_path = package["readme"]

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
                    f"docs/packages/{pkg_name}/README.html",
                )
                # Normalize any remaining HTML hrefs
                html_content = self.markdown_processor.convert_markdown_links_to_html(
                    html_content, str(readme_path), f"docs/packages/{pkg_name}/README.html"
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

                # Wrap with the standard docs layout. The rail carries the canonical
                # documentation navigation rather than a per-page heading outline.
                nav_html = self.render_docs_sidebar_nav(f"docs/packages/{pkg_name}/README.html")

                wrapped_content = f"""
<section>
   <div class=\"container-fluid\">
    <div class=\"row toc-layout\">
      <aside class=\"toc-sidebar d-none d-lg-block p-0\">
        <div class=\"position-sticky\">
          {nav_html or '<div class="text-muted small">No sections</div>'}
        </div>
      </aside>
      <div class=\"container-content\">
        {html_content}
      </div>
    </div>
    </div>
</section>
"""

                output_path = f"docs/packages/{pkg_name}/README.html"
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

        self.build_legacy_package_redirects()

    def discover_package_docs(self) -> list[dict]:
        """Return package README metadata in deterministic package-name order."""
        packages: list[dict] = []
        packages_dir = Path("packages")
        if not packages_dir.exists():
            return packages

        for package_dir in packages_dir.iterdir():
            pyproject_path = package_dir / "pyproject.toml"
            readme_path = package_dir / "README.md"
            if not package_dir.is_dir() or not pyproject_path.exists() or not readme_path.exists():
                continue
            try:
                import tomllib

                data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
                project = data.get("project", {})
                package_name = project.get("name", package_dir.name)
                description = project.get("description", "")
            except (OSError, ValueError):
                package_name = package_dir.name
                description = ""
            packages.append(
                {
                    "directory": package_dir.name,
                    "name": package_name,
                    "description": description,
                    "readme": readme_path,
                }
            )
        return sorted(packages, key=lambda package: package["name"])

    def build_legacy_package_redirects(self) -> None:
        """Preserve the published package URLs used by the predecessor site."""
        redirects = {
            "core": "harborrag-core",
            "mcp-server": "harborrag-mcp-server",
            "qdrant-loader": "harborrag",  # branding-compat: preserve the published URL
        }
        for legacy_slug, package_slug in redirects.items():
            target_page = self.output_dir / "docs" / "packages" / package_slug / "README.html"
            if not target_page.exists():
                continue
            target = f"../{package_slug}/README.html"
            output_path = self.output_dir / "docs" / "packages" / legacy_slug / "README.html"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                '<!doctype html><html lang="en"><head>'
                '<meta charset="utf-8">'
                f'<meta http-equiv="refresh" content="0; url={html.escape(target, quote=True)}">'
                f'<link rel="canonical" href="{html.escape(target, quote=True)}">'
                f"<title>Moved to {html.escape(package_slug)}</title></head>"
                f'<body><p>This page moved to <a href="{html.escape(target, quote=True)}">'
                f"{html.escape(package_slug)}</a>.</p></body></html>",
                encoding="utf-8",
            )
            print(f"↪️  Redirected docs/packages/{legacy_slug}/ to {package_slug}/")

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
