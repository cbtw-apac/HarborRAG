"""Regression tests for the coordinated HarborRAG release command."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import release
from click.testing import CliRunner
from release_support import checks, cli, versioning, versions
from release_support.cli import release as release_command
from release_support.config import PRIMARY_PACKAGE, WORKSPACE_PACKAGE

ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]
PUBLIC_PACKAGES = {
    "harborrag",
    "harborrag-adapters",
    "harborrag-app",
    "harborrag-core",
    "harborrag-engine",
    "harborrag-mcp-server",
    "harborrag-memory",
    "harborrag-runtime",
}


def test_release_entrypoint_loads_the_current_workspace() -> None:
    assert set(release.PACKAGES) == PUBLIC_PACKAGES | {WORKSPACE_PACKAGE}
    assert release.get_current_version() == CURRENT_VERSION
    assert set(release.get_all_package_versions().values()) == {CURRENT_VERSION}

    ordered = release.get_packages_for_release()
    assert set(ordered) == PUBLIC_PACKAGES
    assert ordered[-1] == PRIMARY_PACKAGE
    assert "harborrag-memory" in ordered
    assert "harborrag-mcp-server" in ordered


def test_release_cli_help_and_dry_sync_are_non_mutating() -> None:
    runner = CliRunner()
    help_result = runner.invoke(release_command, ["--help"])
    assert help_result.exit_code == 0
    assert "--sync-versions" in help_result.output
    assert "--publish" in help_result.output
    assert "--development-status" in help_result.output

    sync_result = runner.invoke(release_command, ["--sync-versions", "--dry-run"])
    assert sync_result.exit_code == 0, sync_result.output
    assert f"Synchronizing workspace projects to {CURRENT_VERSION}" in sync_result.output


def test_release_cli_preparation_dry_run_never_publishes() -> None:
    candidate = release.calculate_new_version(CURRENT_VERSION, 3)
    result = CliRunner().invoke(
        release_command,
        ["--dry-run", "--bump", "patch"],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert f"Prepare coordinated release: {CURRENT_VERSION} → {candidate}" in result.output
    assert "Future package tags" in result.output
    assert "Published HarborRAG" not in result.output


def test_release_cli_publish_mode_uses_the_reviewed_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "assert_release_files_exist", lambda: None)
    monkeypatch.setattr(cli, "_run_initial_checks", lambda dry_run: {"ready": True})
    monkeypatch.setattr(cli, "get_all_package_versions", lambda: {})
    monkeypatch.setattr(cli, "get_current_version", lambda: "2.1.0")
    monkeypatch.setattr(cli, "check_changelog_updated", lambda version, dry_run: True)
    monkeypatch.setattr(
        cli,
        "check_release_tags_absent",
        lambda packages, version, dry_run: True,
    )

    result = CliRunner().invoke(release_command, ["--publish", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "harborrag-v2.1.0" in result.output
    assert "Prepare coordinated release" not in result.output


@pytest.mark.parametrize(
    ("current", "bump", "expected"),
    [
        ("2.0.0", 1, "3.0.0"),
        ("2.0.0", 2, "2.1.0"),
        ("2.0.0", 3, "2.0.1"),
        ("2.0.0", 4, "2.0.1b1"),
        ("2.0.0b1", 4, "2.0.0b2"),
        ("2.0.0rc1", 3, "2.0.0"),
    ],
)
def test_calculate_new_version(current: str, bump: int, expected: str) -> None:
    assert release.calculate_new_version(current, bump) == expected


def test_calculate_new_version_validates_explicit_versions() -> None:
    assert release.calculate_new_version("2.0.0", 5, "2.1.0rc1") == "2.1.0rc1"
    with pytest.raises(ValueError, match="Custom version"):
        release.calculate_new_version("2.0.0", 5, "release-next")
    with pytest.raises(ValueError, match="Unknown bump"):
        release.calculate_new_version("2.0.0", 9)


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/cbtw-apac/HarborRAG.git",
        "git@github.com:cbtw-apac/HarborRAG.git",
        "ssh://git@github.com/cbtw-apac/HarborRAG.git",
    ],
)
def test_extract_repo_info_accepts_supported_github_remotes(remote: str) -> None:
    assert release.extract_repo_info(remote) == "cbtw-apac/HarborRAG"


def test_extract_repo_info_rejects_non_github_hosts() -> None:
    assert (
        release.extract_repo_info("https://example.com/cbtw-apac/HarborRAG.git", dry_run=True)
        == "unknown/repo"
    )


def test_version_update_preserves_surrounding_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = tmp_path / "sample.toml"
    pyproject.write_text(
        '[project]\nname = "sample"\nversion = "1.0.0"\n# keep this comment\n\n'
        "[tool.sample]\nenabled = true\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(
        versions.PACKAGES,
        "sample",
        {"path": ".", "pyproject": "sample.toml", "create_release": True},
    )
    monkeypatch.setattr(versions, "repository_path", lambda relative: tmp_path / relative)

    versions.update_package_version("sample", "1.1.0")

    source = pyproject.read_text(encoding="utf-8")
    assert 'version = "1.1.0"' in source
    assert "# keep this comment" in source
    assert tomllib.loads(source)["tool"]["sample"]["enabled"] is True


def test_internal_dependency_update_preserves_extras_and_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = tmp_path / "sample.toml"
    pyproject.write_text(
        '[project]\nname = "sample"\nversion = "1.0.0"\n'
        "dependencies = [\"harborrag-core[crypto]>=1; python_version >= '3.12'\", "
        '"requests>=2"]\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(
        versioning.PACKAGES,
        "sample",
        {"path": ".", "pyproject": "sample.toml", "create_release": True},
    )
    monkeypatch.setattr(versioning, "repository_path", lambda relative: tmp_path / relative)

    changes = versioning.update_internal_dependencies_for_package(
        "sample", {"harborrag-core"}, "2.1.0"
    )

    assert changes == [
        (
            "harborrag-core[crypto]>=1; python_version >= '3.12'",
            "harborrag-core[crypto]==2.1.0; python_version >= '3.12'",
        )
    ]
    dependencies = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    assert dependencies == [
        "harborrag-core[crypto]==2.1.0; python_version >= '3.12'",
        "requests>=2",
    ]


def test_final_version_preserves_explicit_alpha_maturity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = (
        '[project]\nname = "sample"\nversion = "2.0.0"\nclassifiers = [\n'
        '  "Development Status :: 3 - Alpha",\n]\n'
    )
    (tmp_path / "sample.toml").write_text(project, encoding="utf-8")
    (tmp_path / "workspace.toml").write_text(project, encoding="utf-8")
    monkeypatch.setitem(
        versioning.PACKAGES,
        "sample",
        {"path": ".", "pyproject": "sample.toml", "create_release": True},
    )
    monkeypatch.setitem(
        versioning.PACKAGES,
        WORKSPACE_PACKAGE,
        {"path": ".", "pyproject": "workspace.toml", "create_release": False},
    )
    monkeypatch.setattr(versioning, "repository_path", lambda relative: tmp_path / relative)

    versioning.update_development_status_classifier("sample", "2.1.0")
    assert "Development Status :: 3 - Alpha" in (tmp_path / "sample.toml").read_text(
        encoding="utf-8"
    )

    versioning.update_development_status_classifier("sample", "2.1.0b1")
    assert "Development Status :: 4 - Beta" in (tmp_path / "sample.toml").read_text(
        encoding="utf-8"
    )


def test_explicit_maturity_adds_a_missing_classifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = tmp_path / "sample.toml"
    pyproject.write_text(
        '[project]\nname = "sample"\nversion = "2.0.0"\n\n[build-system]\n'
        'requires = ["hatchling"]\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(
        versioning.PACKAGES,
        "sample",
        {"path": ".", "pyproject": "sample.toml", "create_release": True},
    )
    monkeypatch.setattr(versioning, "repository_path", lambda relative: tmp_path / relative)

    versioning.update_development_status_classifier("sample", "2.1.0", development_status="beta")

    classifiers = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["classifiers"]
    assert classifiers == ["Development Status :: 4 - Beta"]


def test_changelog_gate_checks_the_first_released_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [2.1.0] - 2026-08-14\n\nReady.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(checks, "REPOSITORY_ROOT", tmp_path)
    assert checks.check_changelog_updated("2.1.0")
    assert not checks.check_changelog_updated("2.2.0", dry_run=True)
    assert checks.extract_changelog_for_version("2.1.0") == "Ready."


def test_release_workflows_cover_every_release_package() -> None:
    publish_workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    test_workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    for package_name in release.get_packages_for_release():
        assert package_name in publish_workflow
        assert package_name in test_workflow
