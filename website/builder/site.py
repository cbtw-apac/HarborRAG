"""SiteBuildMixin implementation for the website builder."""

import json
from datetime import UTC
from pathlib import Path


class SiteBuildMixin:
    """Focused website-build operations composed by ``WebsiteBuilder``."""

    def build_site(
        self,
        coverage_artifacts_dir: str | None = None,
        test_results_dir: str | None = None,
    ) -> None:
        """Build the complete website."""
        print("🏗️  Building HarborRAG website...")

        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Copy assets first
        self.copy_assets()

        # Generate project info
        project_info = self.generate_project_info()
        self.markdown_processor.repository_url = project_info["github_url"]
        self.generate_web_manifest(project_info)

        # Parse the repository-owned table of contents once for all navigation views.
        self.build_docs_nav()

        # Build main pages
        self.build_page(
            "base.html",
            "index.html",
            "Open-source RAG infrastructure for engineering knowledge",
            "Connect engineering systems, understand complex documents, and serve governed retrieval through one modular open-source RAG stack.",
            "index.html",
            homepage_navigation=self.render_docs_navigation(compact=True),
        )

        # Build a friendly 404 page
        try:
            self.build_page(
                "base.html",
                "404.html",
                "Page Not Found",
                "The page you are looking for does not exist.",
                "404.html",
                content=self.load_template("404.html"),
            )
        except Exception as e:
            print(f"⚠️  Failed to build 404 page: {e}")

        # Build docs structure and pages
        _docs_structure = self.build_docs_structure()

        # Create docs directory and index
        docs_output_dir = self.output_dir / "docs"
        docs_output_dir.mkdir(exist_ok=True)

        # Build docs index page using dedicated template content
        self.build_page(
            "base.html",
            "docs/index.html",
            "Documentation",
            "HarborRAG Documentation",
            "docs/index.html",
            content=self.load_template("docs-index.html"),
            docs_navigation=self.render_docs_navigation(),
        )

        # Bridge root docs from repository top-level files
        try:
            if Path("README.md").exists():
                self.build_markdown_page("README.md", "docs/README.html")
            if Path("CHANGELOG.md").exists():
                self.build_markdown_page("CHANGELOG.md", "docs/CHANGELOG.html")
            if Path("CONTRIBUTING.md").exists():
                self.build_markdown_page("CONTRIBUTING.md", "docs/CONTRIBUTING.html")
            if Path("SECURITY.md").exists():
                self.build_markdown_page("SECURITY.md", "docs/SECURITY.html")
            # License (plain text) rendered via helper
            if Path("LICENSE").exists():
                self.build_license_page("LICENSE", "docs/LICENSE.html", "License", "License")
            # Privacy policy page from template
            try:
                privacy_template_path = self.templates_dir / "privacy-policy.html"
                privacy_last_updated = self.get_git_timestamp(str(privacy_template_path))
                if privacy_last_updated:
                    privacy_last_updated = privacy_last_updated.split("T", 1)[0]
                else:
                    from datetime import datetime

                    # Use stable template mtime fallback instead of build date.
                    privacy_last_updated = (
                        datetime.fromtimestamp(privacy_template_path.stat().st_mtime, tz=UTC)
                        .date()
                        .isoformat()
                    )

                self.build_page(
                    "base.html",
                    "privacy-policy.html",
                    "Privacy Policy",
                    "Privacy policy for HarborRAG",
                    "privacy-policy.html",
                    content=self.load_template("privacy-policy.html"),
                    last_updated=privacy_last_updated,
                )
            except FileNotFoundError:
                pass
        except Exception as e:
            print(f"⚠️  Failed to build root docs pages: {e}")

        # Build package README documentation into docs/packages
        try:
            self.build_package_docs()
        except Exception as e:
            print(f"⚠️  Failed to build package docs: {e}")

        # Always create coverage directory and ensure index.html exists
        coverage_output_dir = self.output_dir / "coverage"
        coverage_output_dir.mkdir(exist_ok=True)

        # Build coverage reports if provided
        if coverage_artifacts_dir:
            _coverage_structure = self.build_coverage_structure(coverage_artifacts_dir)

            # Copy coverage artifacts
            coverage_path = Path(coverage_artifacts_dir)
            if coverage_path.exists():
                import shutil

                for item in coverage_path.iterdir():
                    if item.is_file():
                        shutil.copy2(item, coverage_output_dir / item.name)
                    elif item.is_dir():
                        shutil.copytree(item, coverage_output_dir / item.name, dirs_exist_ok=True)
        else:
            # Create styled placeholder coverage index if no artifacts provided
            placeholder_html = (
                '<section class="py-5"><div class="container">'
                '<h1 class="display-5 fw-bold text-primary"><i class="bi bi-graph-up me-2"></i>Coverage Reports</h1>'
                '<div class="alert alert-info mt-4">No coverage artifacts available.</div>'
                "</div></section>"
            )
            self.build_page(
                "base.html",
                "coverage/index.html",
                "Coverage Reports",
                "Test coverage analysis",
                "coverage/index.html",
                content=placeholder_html,
            )

        # Generate directory indexes
        self.generate_directory_indexes()

        # Generate SEO files
        # Build a dynamic sitemap including all HTML pages
        try:
            self.generate_dynamic_sitemap()
        except Exception as e:
            print(f"⚠️  Failed to generate dynamic sitemap: {e}")

        # Always (re)write robots.txt pointing to the sitemap
        try:
            self.generate_robots_file()
        except Exception as e:
            print(f"⚠️  Failed to generate robots.txt: {e}")

        # Create .nojekyll file for GitHub Pages
        nojekyll_path = self.output_dir / ".nojekyll"
        nojekyll_path.touch()
        print("📄 Created .nojekyll file")

        print("✅ Website build completed successfully!")

    def generate_web_manifest(self, project_info: dict) -> None:
        """Write the PWA manifest from canonical project metadata."""
        manifest = {
            "name": project_info["name"],
            "short_name": project_info["name"],
            "description": project_info["description"],
            "start_url": "../",
            "scope": "../",
            "display": "standalone",
            "background_color": "#07131f",
            "theme_color": "#0b7285",
            "icons": [
                {
                    "src": "favicons/android-chrome-192x192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                },
                {
                    "src": "favicons/android-chrome-512x512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                },
                # branding-compat: company FE will replace the existing artwork.
                {
                    "src": "icons/qdrant-loader-icon-static.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                },
            ],
        }
        manifest_path = self.output_dir / "assets" / "site.webmanifest"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
