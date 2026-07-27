#!/usr/bin/env python3
"""Tests for the argparse entry point in website/build.py."""

import sys


class TestBuildCli:
    """Test the CLI wiring between argparse and WebsiteBuilder."""

    def test_main_builds_site_and_returns_zero(
        self, mock_project_structure, monkeypatch, build_main
    ):
        monkeypatch.chdir(mock_project_structure)
        monkeypatch.setattr(
            sys,
            "argv",
            ["build.py", "--output", "site", "--templates", "website/templates"],
        )

        assert build_main() == 0
        assert (mock_project_structure / "site" / "index.html").exists()

    def test_main_honours_base_url_and_marks_it_user_set(
        self, mock_project_structure, monkeypatch, build_main, website_builder_cls
    ):
        monkeypatch.chdir(mock_project_structure)
        monkeypatch.setattr(
            sys,
            "argv",
            ["build.py", "--output", "site", "--base-url", "https://example.test/"],
        )
        captured = {}
        original_build_site = website_builder_cls.build_site

        def spy(self, coverage_artifacts_dir=None, test_results_dir=None):
            captured["base_url"] = self.base_url
            captured["user_set"] = self.base_url_user_set
            return original_build_site(self, coverage_artifacts_dir, test_results_dir)

        monkeypatch.setattr(website_builder_cls, "build_site", spy)

        assert build_main() == 0
        assert captured["base_url"] == "https://example.test/"
        assert captured["user_set"] is True

    def test_main_passes_artifact_directories_through(
        self, mock_project_structure, monkeypatch, build_main, website_builder_cls
    ):
        monkeypatch.chdir(mock_project_structure)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "build.py",
                "--coverage-artifacts",
                "coverage-artifacts",
                "--test-results",
                "test-results",
            ],
        )
        captured = {}

        def fake_build_site(self, coverage_artifacts_dir=None, test_results_dir=None):
            captured["coverage"] = coverage_artifacts_dir
            captured["results"] = test_results_dir

        monkeypatch.setattr(website_builder_cls, "build_site", fake_build_site)

        assert build_main() == 0
        assert captured == {"coverage": "coverage-artifacts", "results": "test-results"}

    def test_main_returns_one_when_build_fails(
        self, mock_project_structure, monkeypatch, capsys, build_main, website_builder_cls
    ):
        monkeypatch.chdir(mock_project_structure)
        monkeypatch.setattr(sys, "argv", ["build.py"])

        def exploding_build_site(self, coverage_artifacts_dir=None, test_results_dir=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(website_builder_cls, "build_site", exploding_build_site)

        assert build_main() == 1
        assert "Build failed" in capsys.readouterr().out
