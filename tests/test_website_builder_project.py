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

        assert info["name"] == "QDrant Loader"
        assert info["version"] == "1.2.3"

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

        assert builder.base_url == "https://home.example.test"
        assert info["github_url"] == "https://github.com/example/demo"

    def test_respects_user_set_base_url(self, builder):
        Path("pyproject.toml").write_text(
            '[project]\nname = "demo"\n[project.urls]\nHomepage = "https://home.example.test/"\n',
            encoding="utf-8",
        )
        builder.base_url_user_set = True

        builder.generate_project_info()

        assert builder.base_url == ""

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

        assert info["name"] == "QDrant Loader"
        assert info["version"] == "0.4.0b1"

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

    def test_build_docs_nav_lists_files_and_directories(self, builder):
        Path("docs/guides").mkdir(parents=True, exist_ok=True)
        Path("docs/guides/nested.md").write_text("# Nested", encoding="utf-8")

        nav = builder.build_docs_nav()

        urls = {child["url"] for child in nav["children"]}
        assert "docs/installation.md" in urls
        assert "docs/guides/" in urls
        assert builder.docs_nav_data == nav

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
