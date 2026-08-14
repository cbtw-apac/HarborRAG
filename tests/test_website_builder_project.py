#!/usr/bin/env python3
"""Tests for website/builder/project.py — project metadata and docs navigation."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


class TestGenerateProjectInfo:
    """Test project metadata assembly from pyproject.toml and git."""

    def test_reads_pyproject(self, builder):
        info = builder.generate_project_info()

        assert info["name"] == "qdrant-loader"
        assert info["version"] == "0.4.0"
        written = json.loads((builder.output_dir / "project-info.json").read_text(encoding="utf-8"))
        assert written["version"] == "0.4.0"

    def test_normalizes_workspace_name(self, builder):
        Path("pyproject.toml").write_text(
            '[project]\nname = "harborrag-workspace"\nversion = "1.2.3"\n', encoding="utf-8"
        )

        info = builder.generate_project_info()

        assert info["name"] == "HarborRAG"
        assert info["version"] == "1.2.3"

    def test_reads_public_release_status(self, builder):
        Path("pyproject.toml").write_text(
            '[project]\nname = "harborrag-workspace"\nversion = "2.0.0"\n'
            'classifiers = ["Development Status :: 3 - Alpha"]\n',
            encoding="utf-8",
        )

        info = builder.generate_project_info()

        assert info["status"] == "Alpha"
        assert info["version"] == "2.0.0"

    def test_picks_up_urls(self, builder):
        Path("pyproject.toml").write_text(
            "[project]\n"
            'name = "demo"\n'
            'version = "9.9.9"\n'
            "[project.urls]\n"
            'Homepage = "https://home.example.test/"\n'
            'Repository = "https://github.com/example/demo"\n',
            encoding="utf-8",
        )

        info = builder.generate_project_info()

        assert builder.base_url == ""
        assert info["github_url"] == "https://github.com/example/demo"
        assert info["issues_url"] == "https://github.com/example/demo/issues"

    def test_falls_back_to_source_url(self, builder):
        Path("pyproject.toml").write_text(
            '[project]\nname = "demo"\n[project.urls]\nSource = "https://git.example.test/demo"\n',
            encoding="utf-8",
        )

        info = builder.generate_project_info()

        assert info["github_url"] == "https://git.example.test/demo"

    def test_tolerates_malformed_pyproject(self, builder):
        Path("pyproject.toml").write_text("this is not valid toml {{{", encoding="utf-8")

        info = builder.generate_project_info()

        assert info["name"] == "HarborRAG"
        assert info["version"] == "2.0.0"
        assert info["status"] == "Alpha"

    def test_includes_git_metadata(self, tmp_path, project_root_dir, website_builder_cls):
        """Run against the real repository so the git lookups succeed."""
        if not (project_root_dir / ".git").exists():
            pytest.skip("Not a git checkout")

        original_cwd = os.getcwd()
        os.chdir(project_root_dir)
        try:
            local_builder = website_builder_cls("website/templates", str(tmp_path / "site"))
            info = local_builder.generate_project_info()
        finally:
            os.chdir(original_cwd)

        assert info["commit_hash"]
        assert info["commit"]["short"] == info["commit_hash"][:7]
        assert info["commit_date"]
        assert info["build"]["timestamp"].endswith("Z")

    def test_survives_missing_git(self, builder, monkeypatch):
        def exploding_run(*args, **kwargs):
            raise FileNotFoundError("git not installed")

        monkeypatch.setattr(subprocess, "run", exploding_run)

        info = builder.generate_project_info()

        assert info["commit"]["hash"] == ""
        assert info["commit"]["short"] == ""


class TestDocsNavigation:
    """Test docs navigation and structure construction."""

    def test_build_docs_nav_returns_empty_without_docs_dir(self, builder):
        shutil.rmtree("docs")

        assert builder.build_docs_nav() == {}

    def test_build_docs_nav_uses_canonical_toc_order(self, builder):
        Path("docs/TOC.md").write_text(
            "# Docs\n\n"
            "## Getting started\n\n"
            "- [Installation](installation.md) — Install HarborRAG\n"
            "- [Package](../packages/harborrag/README.md)\n",
            encoding="utf-8",
        )

        nav = builder.build_docs_nav()

        assert [section["title"] for section in nav["children"]] == ["Getting started"]
        assert [item["url"] for item in nav["children"][0]["children"]] == [
            "installation.html",
            "packages/harborrag/README.html",
        ]
        assert nav["children"][0]["children"][0]["description"] == "Install HarborRAG"
        assert builder.docs_nav_data == nav

    def test_render_docs_navigation_prefixes_homepage_links(self, builder):
        Path("docs/TOC.md").write_text(
            "# Docs\n\n## Guides\n\n- [Quick start](getting-started/quick-start.md)\n",
            encoding="utf-8",
        )

        html = builder.render_docs_navigation(compact=True)

        assert 'href="docs/getting-started/quick-start.html"' in html

    def test_render_docs_navigation_reports_missing_toc(self, builder):
        Path("docs/TOC.md").unlink(missing_ok=True)

        html = builder.render_docs_navigation()

        assert html == '<p class="text-muted">Documentation navigation is unavailable.</p>'

    def test_build_docs_structure_without_docs_dir(self, builder):
        shutil.rmtree("docs")

        structure = builder.build_docs_structure()

        assert structure == {"title": "Documentation", "children": []}
        assert (builder.output_dir / "docs").exists()

    def test_build_docs_structure_builds_pages(self, builder):
        structure = builder.build_docs_structure()

        urls = {child["url"] for child in structure["children"]}
        assert "docs/installation.html" in urls
        assert (builder.output_dir / "docs" / "installation.html").exists()

    def test_build_docs_structure_reports_page_failures(self, builder, capsys, monkeypatch):
        def exploding_build(*args, **kwargs):
            raise RuntimeError("render failed")

        monkeypatch.setattr(builder, "build_markdown_page", exploding_build)

        structure = builder.build_docs_structure()

        assert structure["children"]
        assert "Failed to build docs page" in capsys.readouterr().out
