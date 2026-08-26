#!/usr/bin/env python3
"""Tests for website/builder/pages.py — page, markdown-page and license-page builds."""

from pathlib import Path

import pytest


class TestPageBuildMixin:
    """Test single-page, markdown-page and license-page construction."""

    def test_build_page_uses_absolute_base_url_at_site_root(self, builder):
        builder.base_url = "https://docs.example.test/"
        builder.build_page("base.html", "index.html", "Home", "Landing", "index.html")

        html = (builder.output_dir / "index.html").read_text(encoding="utf-8")
        assert "https://docs.example.test/index.html" in html

    def test_build_page_uses_relative_base_url_for_nested_pages(self, builder):
        builder.build_page(
            "base.html",
            "docs/deep/page.html",
            "Deep",
            "Deep page",
            "docs/deep/page.html",
            "<p>x</p>",
        )

        assert (builder.output_dir / "docs" / "deep" / "page.html").exists()

    def test_build_page_uses_public_default_for_canonical_url(self, builder):
        builder.build_page("base.html", "docs/page.html", "Page", "Docs", "docs/page.html")

        html = (builder.output_dir / "docs" / "page.html").read_text(encoding="utf-8")
        assert 'href="https://cbtw-apac.github.io/HarborRAG/docs/page.html"' in html

    def test_build_page_falls_back_to_empty_content_for_canonical_page(self, builder):
        # No "missing.html" template exists, but output == canonical so it is tolerated.
        builder.build_page("base.html", "missing.html", "Missing", "Missing", "missing.html")

        assert (builder.output_dir / "missing.html").exists()

    def test_build_page_raises_when_content_template_missing(self, builder):
        with pytest.raises(FileNotFoundError):
            builder.build_page("base.html", "no-such-template.html", "T", "D", "other.html")

    def test_build_markdown_page_skips_missing_file(self, builder, capsys):
        builder.build_markdown_page("does/not/exist.md", "docs/nope.html")

        assert "Markdown file not found" in capsys.readouterr().out
        assert not (builder.output_dir / "docs" / "nope.html").exists()

    def test_build_markdown_page_reports_read_failures(self, builder, capsys, monkeypatch):
        source = Path("docs/unreadable.md")
        source.write_text("# Title", encoding="utf-8")

        real_open = open

        def exploding_open(file, *args, **kwargs):
            if str(file).endswith("unreadable.md"):
                raise OSError("permission denied")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", exploding_open)

        builder.build_markdown_page(str(source), "docs/unreadable.html")

        assert "Failed to read markdown file" in capsys.readouterr().out

    def test_build_markdown_page_derives_title_and_toc(self, builder):
        source = Path("docs/guide.md")
        source.write_text("# Guide\n\n## Section One\n\nBody text.\n", encoding="utf-8")

        builder.build_markdown_page(str(source), "docs/guide.html")

        html = (builder.output_dir / "docs" / "guide.html").read_text(encoding="utf-8")
        assert "Guide" in html
        assert "toc-sidebar" in html

    def test_build_markdown_page_without_headings_renders_no_sections(self, builder):
        source = Path("docs/plain.md")
        source.write_text("Just a paragraph.\n", encoding="utf-8")

        builder.build_markdown_page(str(source), "docs/plain.html", title="Plain")

        html = (builder.output_dir / "docs" / "plain.html").read_text(encoding="utf-8")
        assert "No sections" in html

    def test_build_markdown_page_links_repository_files_to_github(self, builder):
        Path("config").mkdir(exist_ok=True)
        Path("config/example.yaml").write_text("enabled: true\n", encoding="utf-8")
        source = Path("docs/repository-link.md")
        source.write_text("# Config\n\n[Example](../config/example.yaml)\n", encoding="utf-8")

        builder.build_markdown_page(str(source), "docs/repository-link.html")

        html = (builder.output_dir / "docs" / "repository-link.html").read_text(encoding="utf-8")
        assert 'href="https://github.com/cbtw-apac/HarborRAG/blob/main/config/example.yaml"' in html

    def test_build_license_page_skips_missing_license(self, builder, capsys):
        builder.build_license_page("NOPE-LICENSE", "license.html")

        assert "License file not found" in capsys.readouterr().out
        assert not (builder.output_dir / "license.html").exists()

    def test_build_license_page_renders_license_text(self, builder):
        Path("LICENSE").write_text("GPL-3.0 terms here", encoding="utf-8")

        builder.build_license_page("LICENSE", "license.html", "License", "Project license")

        html = (builder.output_dir / "license.html").read_text(encoding="utf-8")
        assert "License Information" in html
        assert "GPL-3.0 terms here" in html

    def test_build_license_page_reports_build_failures(self, builder, capsys, monkeypatch):
        Path("LICENSE").write_text("terms", encoding="utf-8")

        def exploding_build_page(*args, **kwargs):
            raise RuntimeError("template exploded")

        monkeypatch.setattr(builder, "build_page", exploding_build_page)

        builder.build_license_page("LICENSE", "license.html")

        assert "Failed to build license page" in capsys.readouterr().out
