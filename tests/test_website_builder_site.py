#!/usr/bin/env python3
"""Tests for website/builder/site.py — the end-to-end build_site orchestration."""

from pathlib import Path


class TestSiteBuildMixin:
    """Test the end-to-end build_site orchestration."""

    def test_produces_the_core_pages(self, builder):
        builder.build_site()

        for relative in ("index.html", "docs/index.html", "coverage/index.html", ".nojekyll"):
            assert (builder.output_dir / relative).exists(), relative

        coverage_index = (builder.output_dir / "coverage" / "index.html").read_text(
            encoding="utf-8"
        )
        assert "No coverage artifacts available." in coverage_index

    def test_bridges_root_documents(self, builder):
        Path("CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n", encoding="utf-8")
        Path("CONTRIBUTING.md").write_text("# Contributing\n\n## Setup\n", encoding="utf-8")
        Path("LICENSE").write_text("GPL-3.0", encoding="utf-8")

        builder.build_site()

        docs = builder.output_dir / "docs"
        assert (docs / "README.html").exists()
        assert (docs / "CHANGELOG.html").exists()
        assert (docs / "CONTRIBUTING.html").exists()
        assert "GPL-3.0" in (docs / "LICENSE.html").read_text(encoding="utf-8")

    def test_renders_privacy_policy_with_git_timestamp(self, builder, monkeypatch):
        monkeypatch.setattr(builder, "get_git_timestamp", lambda path: "2026-03-04T05:06:07+00:00")

        builder.build_site()

        html = (builder.output_dir / "privacy-policy.html").read_text(encoding="utf-8")
        assert "2026-03-04" in html

    def test_falls_back_to_template_mtime(self, builder, monkeypatch):
        monkeypatch.setattr(builder, "get_git_timestamp", lambda path: "")

        builder.build_site()

        html = (builder.output_dir / "privacy-policy.html").read_text(encoding="utf-8")
        assert "Last updated: 20" in html

    def test_reports_404_template_failures(self, builder, capsys):
        Path("website/templates/404.html").unlink(missing_ok=True)

        builder.build_site()

        assert "Failed to build 404 page" in capsys.readouterr().out

    def test_renders_404_when_template_exists(self, builder):
        Path("website/templates/404.html").write_text("<h1>Missing</h1>", encoding="utf-8")

        builder.build_site()

        assert "Missing" in (builder.output_dir / "404.html").read_text(encoding="utf-8")

    def test_reports_root_doc_failures(self, builder, capsys, monkeypatch):
        def exploding_markdown_page(*args, **kwargs):
            raise RuntimeError("markdown exploded")

        monkeypatch.setattr(builder, "build_markdown_page", exploding_markdown_page)

        builder.build_site()

        assert "Failed to build root docs pages" in capsys.readouterr().out

    def test_reports_package_doc_failures(self, builder, capsys, monkeypatch):
        def exploding_package_docs():
            raise RuntimeError("packages exploded")

        monkeypatch.setattr(builder, "build_package_docs", exploding_package_docs)

        builder.build_site()

        assert "Failed to build package docs" in capsys.readouterr().out

    def test_copies_coverage_artifacts(self, builder, sample_coverage_data):
        (sample_coverage_data / "summary.txt").write_text("all good", encoding="utf-8")

        builder.build_site(coverage_artifacts_dir=str(sample_coverage_data))

        coverage_dir = builder.output_dir / "coverage"
        assert (coverage_dir / "summary.txt").exists()
        assert (coverage_dir / "htmlcov-loader" / "index.html").exists()
        assert (coverage_dir / "loader" / "index.html").exists()

    def test_tolerates_a_missing_coverage_directory(self, builder):
        builder.build_site(coverage_artifacts_dir="absent-coverage-dir")

        assert (builder.output_dir / "coverage").is_dir()

    def test_reports_sitemap_failures(self, builder, capsys, monkeypatch):
        def exploding_sitemap():
            raise RuntimeError("sitemap exploded")

        monkeypatch.setattr(builder, "generate_dynamic_sitemap", exploding_sitemap)

        builder.build_site()

        assert "Failed to generate dynamic sitemap" in capsys.readouterr().out

    def test_reports_robots_failures(self, builder, capsys, monkeypatch):
        def exploding_robots():
            raise RuntimeError("robots exploded")

        monkeypatch.setattr(builder, "generate_robots_file", exploding_robots)

        builder.build_site()

        assert "Failed to generate robots.txt" in capsys.readouterr().out
