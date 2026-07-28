#!/usr/bin/env python3
"""Tests for website/builder/package_docs.py — package READMEs and directory indexes."""

import shutil
from pathlib import Path


def write_package_readme(name: str, body: str) -> None:
    """Create packages/<name>/README.md relative to the current working directory."""
    readme = Path("packages") / name / "README.md"
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text(body, encoding="utf-8")


class TestBuildPackageDocs:
    """Test rendering package README files into docs/packages."""

    def test_skips_absent_packages(self, builder):
        builder.build_package_docs()

        assert not (builder.output_dir / "docs" / "packages").exists()

    def test_renders_known_packages(self, builder):
        write_package_readme("qdrant-loader", "# QDrant Loader\n\n## Usage\n\nInstall it.\n")
        write_package_readme("qdrant-loader-mcp-server", "# MCP\n\n## Tools\n\nList.\n")
        write_package_readme("qdrant-loader-core", "# Core\n\n## API\n\nStuff.\n")

        builder.build_package_docs()

        packages_dir = builder.output_dir / "docs" / "packages"
        assert (packages_dir / "qdrant-loader" / "README.html").exists()
        assert (packages_dir / "mcp-server" / "README.html").exists()
        assert (packages_dir / "core" / "README.html").exists()

        html = (packages_dir / "qdrant-loader" / "README.html").read_text(encoding="utf-8")
        assert "toc-sidebar" in html

    def test_rewrites_relative_repository_links(self, builder):
        write_package_readme(
            "qdrant-loader",
            "# Pkg\n\n"
            "[contributing](../../CONTRIBUTING.md)\n\n"
            "[license](../../LICENSE)\n\n"
            "[guide](../../docs/users/guide.md)\n\n"
            "[anchor](../../docs/users/guide.md#setup)\n",
        )

        builder.build_package_docs()

        html = (
            builder.output_dir / "docs" / "packages" / "qdrant-loader" / "README.html"
        ).read_text(encoding="utf-8")
        # LICENSE carries no extension for the link normalizer to rewrite, so the
        # package-README hardening pass is what absolutises it.
        assert 'href="/docs/LICENSE.html"' in html
        # Everything else is already converted to .html by the link normalizer that
        # runs before the hardening pass, and stays relative.
        assert 'href="../../CONTRIBUTING.html"' in html
        assert 'href="../../users/guide.html"' in html
        assert 'href="../../users/guide.html#setup"' in html
        assert ".md" not in html

    def test_renders_readme_without_headings(self, builder):
        write_package_readme("qdrant-loader-core", "Just prose, no headings.\n")

        builder.build_package_docs()

        html = (builder.output_dir / "docs" / "packages" / "core" / "README.html").read_text(
            encoding="utf-8"
        )
        assert "No sections" in html

    def test_reports_failures(self, builder, capsys, monkeypatch):
        write_package_readme("qdrant-loader", "# Pkg\n")

        def exploding_build_page(*args, **kwargs):
            raise RuntimeError("render exploded")

        monkeypatch.setattr(builder, "build_page", exploding_build_page)

        builder.build_package_docs()

        assert "Failed to build docs for package qdrant-loader" in capsys.readouterr().out


class TestGenerateDirectoryIndexes:
    """Test index.html generation for documentation directories."""

    def test_handles_absent_directories(self, builder):
        shutil.rmtree("docs")

        builder.generate_directory_indexes()  # must not raise

    def test_from_source_markdown(self, builder):
        Path("docs/cli-reference").mkdir(parents=True, exist_ok=True)
        Path("docs/cli-reference/README.md").write_text("# CLI\n\nUsage.\n", encoding="utf-8")

        builder.generate_directory_indexes()

        assert (builder.output_dir / "docs" / "cli-reference" / "index.html").exists()

    def test_prefers_index_md_over_html(self, builder):
        target = Path("docs/faq")
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.md").write_text("# FAQ\n\nAnswers.\n", encoding="utf-8")

        builder.generate_directory_indexes()

        html = (builder.output_dir / "docs" / "faq" / "index.html").read_text(encoding="utf-8")
        assert "FAQ" in html

    def test_copies_source_html(self, builder):
        target = Path("docs/reference")
        target.mkdir(parents=True, exist_ok=True)
        (target / "README.html").write_text("<h1>Reference</h1>", encoding="utf-8")

        builder.generate_directory_indexes()

        html = (builder.output_dir / "docs" / "reference" / "index.html").read_text(
            encoding="utf-8"
        )
        assert "Reference" in html

    def test_rewrites_site_html(self, builder, capsys):
        site_dir = builder.output_dir / "docs" / "api"
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "README.html").write_text("<h1>API</h1>", encoding="utf-8")

        builder.generate_directory_indexes()

        index = (site_dir / "index.html").read_text(encoding="utf-8")
        assert index == "<h1>API</h1>"
        assert "Generated index.html" in capsys.readouterr().out

    def test_reports_failures(self, builder, capsys, monkeypatch):
        Path("docs/broken").mkdir(parents=True, exist_ok=True)
        Path("docs/broken/README.md").write_text("# Broken\n", encoding="utf-8")

        def exploding_build(*args, **kwargs):
            raise RuntimeError("nope")

        monkeypatch.setattr(builder, "build_markdown_page", exploding_build)

        builder.generate_directory_indexes()

        assert "Failed to generate index for" in capsys.readouterr().out
