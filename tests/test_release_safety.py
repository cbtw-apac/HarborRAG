"""Safety and retry regressions for coordinated release automation."""

from pathlib import Path

import pytest
from release_support import checks, cli, releases, versioning, versions, workflows
from release_support.diagnostics import redact_diagnostic
from release_support.github_api import GitHubRequestError, GitHubResponse


def _result(stdout: str = "", stderr: str = "", returncode: int = 0) -> checks.CommandResult:
    return checks.CommandResult(stdout, stderr, returncode)


def _release_response(status: int, payload: object | None = None) -> GitHubResponse:
    return GitHubResponse(status, payload, "response text")


def _matching_release() -> dict[str, object]:
    return {
        "tag_name": "harborrag-v2.0.0",
        "name": "harborrag v2.0.0",
        "draft": False,
        "prerelease": False,
    }


def _prepare_release_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        releases,
        "_command_result",
        lambda _command: _result("https://github.com/cbtw-apac/HarborRAG.git"),
    )
    monkeypatch.setattr(releases, "extract_changelog_for_version", lambda _version: "Notes")


def test_release_diagnostics_redact_tokens_and_url_credentials() -> None:
    diagnostic = (
        "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz123456 "
        "https://user:password@example.test/?token=secret-value"
    )

    redacted = redact_diagnostic(diagnostic)

    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "password" not in redacted
    assert "secret-value" not in redacted
    assert redacted.count("[REDACTED]") == 3


@pytest.mark.parametrize(
    ("current", "bump", "expected"),
    [("2", 3, "2.0.1"), ("2.4", 2, "2.5.0")],
)
def test_version_bumps_normalize_short_pep440_releases(
    current: str, bump: int, expected: str
) -> None:
    assert versioning.calculate_new_version(current, bump) == expected


def test_classifier_update_supports_single_quoted_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = tmp_path / "sample.toml"
    pyproject.write_text(
        "[project]\nname = 'sample'\nversion = '2.0.0'\n"
        "classifiers = ['Development Status :: 3 - Alpha']\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(
        versioning.PACKAGES,
        "sample",
        {"path": ".", "pyproject": "sample.toml", "create_release": True},
    )
    monkeypatch.setattr(versioning, "repository_path", lambda relative: tmp_path / relative)

    versioning.update_development_status_classifier("sample", "2.1.0b1")

    assert "'Development Status :: 4 - Beta'" in pyproject.read_text(encoding="utf-8")


def test_release_file_gate_includes_typescript_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(versions, "PACKAGES", {})
    monkeypatch.setattr(versions, "TYPESCRIPT_PACKAGE", tmp_path / "missing-package.json")

    with pytest.raises(SystemExit):
        versions.assert_release_files_exist()


def test_changelog_gate_requires_unreleased_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [2.0.0] - 2026-08-14\n\nReady.\n", encoding="utf-8"
    )
    monkeypatch.setattr(checks, "REPOSITORY_ROOT", tmp_path)

    assert not checks.check_changelog_updated("2.0.0", dry_run=True)


def test_matching_retry_tags_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    def command(command: str, _dry_run: bool = False) -> checks.CommandResult:
        if command == "git rev-parse HEAD" or command.startswith("git rev-list"):
            return _result("reviewed-commit")
        if command.startswith("git tag --list"):
            return _result("harborrag-v2.0.0")
        raise AssertionError(command)

    monkeypatch.setattr(checks, "_command_result", command)

    assert checks.check_release_tags_absent(["harborrag"], "2.0.0")


def test_retry_tags_on_another_commit_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def command(command: str, _dry_run: bool = False) -> checks.CommandResult:
        if command == "git rev-parse HEAD":
            return _result("reviewed-commit")
        if command.startswith("git tag --list"):
            return _result("harborrag-v2.0.0")
        if command.startswith("git rev-list"):
            return _result("another-commit")
        raise AssertionError(command)

    monkeypatch.setattr(checks, "_command_result", command)

    assert not checks.check_release_tags_absent(["harborrag"], "2.0.0", dry_run=True)


def test_tag_push_names_only_coordinated_release_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[str] = []
    monkeypatch.setattr(cli, "get_packages_for_release", lambda: ["harborrag-core", "harborrag"])

    def command(value: str, _dry_run: bool = False) -> str:
        commands.append(value)
        return ""

    monkeypatch.setattr(cli, "require_command", command)

    cli._tag_release("2.0.0", dry_run=False)

    assert commands[-1] == ("git push origin harborrag-core-v2.0.0 harborrag-v2.0.0")
    assert "--tags" not in commands[-1]


@pytest.mark.parametrize("status", ["queued", "waiting", "pending", "requested"])
def test_workflow_gate_rejects_every_unresolved_status(status: str) -> None:
    runs = [
        {"name": name, "status": status if index == 0 else "completed", "conclusion": "success"}
        for index, name in enumerate(sorted(workflows.CRITICAL_WORKFLOWS))
    ]

    assert "not completed" in str(workflows._completed_workflow_error(runs))


def test_existing_matching_release_makes_retry_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_release_test(monkeypatch)
    calls: list[str] = []

    def request(_repository, endpoint, _token, **_kwargs):
        calls.append(endpoint)
        return _release_response(200, _matching_release())

    monkeypatch.setattr(releases, "github_request", request)

    releases.create_github_release("harborrag", "2.0.0", "token")

    assert calls == ["releases/tags/harborrag-v2.0.0"]


def test_timeout_recovers_when_release_was_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_release_test(monkeypatch)
    responses: list[object] = [
        _release_response(404),
        GitHubRequestError("timed out"),
        _release_response(200, _matching_release()),
    ]

    def request(*_args, **_kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(releases, "github_request", request)

    releases.create_github_release("harborrag", "2.0.0", "token")

    assert not responses


def test_already_exists_response_requires_matching_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_release_test(monkeypatch)
    responses = iter(
        [
            _release_response(404),
            _release_response(422, {"errors": [{"code": "already_exists"}]}),
            _release_response(200, _matching_release()),
        ]
    )
    monkeypatch.setattr(releases, "github_request", lambda *_args, **_kwargs: next(responses))

    releases.create_github_release("harborrag", "2.0.0", "token")


def test_existing_release_metadata_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_release_test(monkeypatch)
    mismatched = {**_matching_release(), "name": "different package v2.0.0"}
    monkeypatch.setattr(
        releases,
        "github_request",
        lambda *_args, **_kwargs: _release_response(200, mismatched),
    )

    with pytest.raises(SystemExit):
        releases.create_github_release("harborrag", "2.0.0", "token")
