#!/usr/bin/env python3
"""Tests for website/builder/coverage.py — coverage artifact ingestion and index."""

import shutil


class TestCoverageBuildMixin:
    """Test coverage artifact ingestion and index rendering."""

    def test_returns_empty_when_no_coverage_dir_given(self, builder):
        result = builder.build_coverage_structure()

        assert result == {"coverage_reports": []}
        assert (builder.output_dir / "coverage").is_dir()

    def test_returns_empty_when_coverage_dir_missing(self, builder):
        result = builder.build_coverage_structure("no-such-coverage-dir")

        assert result == {"coverage_reports": []}

    def test_maps_artifact_directories_to_clean_names(self, builder, sample_coverage_data):
        result = builder.build_coverage_structure(str(sample_coverage_data))

        names = {report["name"] for report in result["coverage_reports"]}
        assert {"loader", "mcp", "website"} <= names
        for report in result["coverage_reports"]:
            assert report["url"] == f"coverage/{report['name']}/index.html"
            assert (builder.output_dir / "coverage" / report["name"] / "index.html").exists()

    def test_renders_coverage_index_for_each_known_package(self, builder, sample_coverage_data):
        core_dir = sample_coverage_data / "htmlcov-core"
        core_dir.mkdir()
        (core_dir / "index.html").write_text("<html>core</html>", encoding="utf-8")

        builder.build_coverage_structure(str(sample_coverage_data))

        index = (builder.output_dir / "coverage" / "index.html").read_text(encoding="utf-8")
        assert "QDrant Loader Core" in index
        assert "MCP Server" in index
        assert "Website" in index
        assert "Core Library" in index
        assert "coverageSummary" in index

    def test_copies_loose_files_and_generic_htmlcov_dirs(self, builder, tmp_path):
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
        generic = artifacts / "htmlcov-engine"
        generic.mkdir()
        (generic / "index.html").write_text("<html>engine</html>", encoding="utf-8")

        result = builder.build_coverage_structure(str(artifacts))

        assert (builder.output_dir / "coverage" / "coverage.xml").exists()
        assert (builder.output_dir / "coverage" / "engine" / "index.html").exists()
        assert [r["name"] for r in result["coverage_reports"]] == ["engine"]

    def test_overwrites_an_existing_destination_directory(self, builder, tmp_path):
        artifacts = tmp_path / "artifacts"
        (artifacts / "htmlcov-loader").mkdir(parents=True)
        (artifacts / "htmlcov-loader" / "index.html").write_text("fresh", encoding="utf-8")

        stale = builder.output_dir / "coverage" / "loader"
        stale.mkdir(parents=True)
        (stale / "stale.html").write_text("stale", encoding="utf-8")

        builder.build_coverage_structure(str(artifacts))

        assert not (stale / "stale.html").exists()
        assert (stale / "index.html").read_text(encoding="utf-8") == "fresh"

    def test_reports_copy_failures_without_aborting(self, builder, tmp_path, capsys, monkeypatch):
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "coverage.xml").write_text("<coverage/>", encoding="utf-8")

        def exploding_copy(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(shutil, "copy2", exploding_copy)

        result = builder.build_coverage_structure(str(artifacts))

        assert result == {"coverage_reports": []}
        assert "Failed to copy coverage file" in capsys.readouterr().out

    def test_directories_without_index_are_not_reported(self, builder, tmp_path):
        artifacts = tmp_path / "artifacts"
        (artifacts / "htmlcov-mcp").mkdir(parents=True)
        (artifacts / "htmlcov-mcp" / "notes.txt").write_text("no index here", encoding="utf-8")

        result = builder.build_coverage_structure(str(artifacts))

        assert result == {"coverage_reports": []}
        assert not (builder.output_dir / "coverage" / "index.html").exists()
