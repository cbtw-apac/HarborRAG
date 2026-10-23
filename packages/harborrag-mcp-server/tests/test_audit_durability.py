"""Durable MCP audit trail: what it writes and every way it refuses an unsafe file."""

from __future__ import annotations

import json
import os
from hashlib import sha256

import pytest

from harborrag_mcp_server import audit
from harborrag_mcp_server.audit import McpAuditLog

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and link contract")


class _OsProxy:
    """Delegate to ``os`` except for the overridden functions, scoped to the audit module."""

    def __init__(self, **overrides: object) -> None:
        self._overrides = overrides

    def __getattr__(self, name: str) -> object:
        return self._overrides.get(name, getattr(os, name))


def _lines(path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_max_entries_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_entries must be positive"):
        McpAuditLog(max_entries=0)


def test_arguments_that_are_not_json_share_one_marker_digest() -> None:
    log = McpAuditLog()
    log.start("tool", {"value": object()}, principal_id="p")
    log.start("tool", {"value": float("nan")}, principal_id="p")
    log.start("tool", {"value": 1}, principal_id="p")

    marker = sha256(b"non-json-arguments").hexdigest()
    digests = [entry["arguments_sha256"] for entry in log.entries]
    assert digests[:2] == [marker, marker]
    assert digests[2] != marker


def test_blank_and_oversized_identifiers_are_bounded() -> None:
    log = McpAuditLog()
    log.start("", {}, principal_id="x" * 300, tenant_id="tenant")

    entry = log.entries[0]
    assert entry["tool"] == "unknown"
    assert entry["principal_id"] == "x" * 256
    assert entry["tenant_id"] == "tenant"


@posix_only
def test_relative_path_is_anchored_at_home_and_records_every_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    log = McpAuditLog(path=audit.Path("trail") / "mcp.jsonl")
    assert log.path == tmp_path / "trail" / "mcp.jsonl"

    invocation = log.start("search", {"q": "x"}, principal_id="owner", tenant_id="t1")
    log.finish(
        invocation,
        "search",
        principal_id="owner",
        outcome="error",
        error_type="TimeoutError",
        tenant_id="t1",
    )
    log.configuration_change(
        action="reload", principal_id="owner", previous_revision="r1", current_revision="r2"
    )

    written = _lines(tmp_path / "trail" / "mcp.jsonl")
    assert [event["event"] for event in written] == [
        "tool_invocation_attempted",
        "tool_invocation_completed",
        "configuration_changed",
    ]
    assert written[1]["error_type"] == "TimeoutError"
    assert written[1]["invocation_id"] == invocation
    assert (written[2]["previous_revision"], written[2]["current_revision"]) == ("r1", "r2")
    assert (tmp_path / "trail").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "trail" / "mcp.jsonl").stat().st_mode & 0o777 == 0o600


def test_append_without_a_path_is_a_configuration_error() -> None:
    with pytest.raises(RuntimeError, match="durable MCP audit path is not configured"):
        McpAuditLog()._append({"event": "x"})


@posix_only
def test_a_write_that_makes_no_progress_fails_instead_of_spinning(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit, "os", _OsProxy(write=lambda _fd, _data: 0))

    with pytest.raises(OSError, match="made no progress"):
        McpAuditLog(path=tmp_path / "audit.jsonl").start("tool", {}, principal_id="p")


@posix_only
def test_hard_linked_audit_file_is_rejected(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")
    os.link(path, tmp_path / "alias.jsonl")

    with pytest.raises(OSError, match="single-link regular file"):
        McpAuditLog(path=path).start("tool", {}, principal_id="p")
    assert path.read_text(encoding="utf-8") == ""


@posix_only
def test_directory_and_file_must_be_owned_by_this_process(tmp_path, monkeypatch) -> None:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "_owned_by_current_process", lambda _metadata: False)
    with pytest.raises(PermissionError, match="directory must be owned"):
        McpAuditLog(path=path).start("tool", {}, principal_id="p")

    answers = iter([True, False])
    monkeypatch.setattr(audit, "_owned_by_current_process", lambda _metadata: next(answers))
    with pytest.raises(PermissionError, match="file must be owned"):
        McpAuditLog(path=path).start("tool", {}, principal_id="p")


@posix_only
def test_swapped_directory_identity_is_rejected(tmp_path, monkeypatch) -> None:
    unrelated = os.stat(tmp_path)
    regular = tmp_path / "plain.txt"
    regular.write_text("x", encoding="utf-8")
    not_a_directory = os.stat(regular)

    monkeypatch.setattr(audit, "os", _OsProxy(fstat=lambda _fd: not_a_directory))
    with pytest.raises(OSError, match="must not contain a symlink or reparse point"):
        McpAuditLog(path=tmp_path / "nested" / "audit.jsonl").start("t", {}, principal_id="p")

    monkeypatch.setattr(audit, "os", _OsProxy(lstat=lambda _path: unrelated))
    with pytest.raises(OSError, match="directory must not be a symbolic link"):
        McpAuditLog(path=tmp_path / "nested" / "audit.jsonl").start("t", {}, principal_id="p")


def test_concurrently_created_directory_is_still_validated(tmp_path, monkeypatch) -> None:
    def mkdir_lost_race(name, mode, *, dir_fd):
        os.mkdir(name, mode, dir_fd=dir_fd)
        raise FileExistsError(name)

    monkeypatch.setattr(audit, "os", _OsProxy(mkdir=mkdir_lost_race))
    path = tmp_path / "raced" / "audit.jsonl"

    McpAuditLog(path=path).start("tool", {}, principal_id="p")

    assert _lines(path)[0]["event"] == "tool_invocation_attempted"


class TestFallbackWithoutDirFd:
    """Platforms without ``openat`` get a best-effort component-by-component check."""

    @pytest.fixture(autouse=True)
    def _no_dir_fd(self, monkeypatch) -> None:
        monkeypatch.setattr(audit, "_SUPPORTS_DIR_FD", False)

    def test_creates_the_directory_and_appends_events(self, tmp_path) -> None:
        path = tmp_path / "fallback" / "audit.jsonl"
        log = McpAuditLog(path=path)
        log.start("tool", {}, principal_id="p")
        log.start("tool", {}, principal_id="q")

        assert [event["principal_id"] for event in _lines(path)] == ["p", "q"]
        assert (tmp_path / "fallback").is_dir()

    @posix_only
    def test_rejects_a_symlinked_component(self, tmp_path) -> None:
        target = tmp_path / "target.jsonl"
        target.write_text("untouched\n", encoding="utf-8")
        link = tmp_path / "link.jsonl"
        link.symlink_to(target)

        with pytest.raises(OSError, match="must not contain a symlink or reparse point"):
            McpAuditLog(path=link).start("tool", {}, principal_id="p")
        assert target.read_text(encoding="utf-8") == "untouched\n"

    @posix_only
    def test_rejects_a_hard_linked_file(self, tmp_path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text("", encoding="utf-8")
        os.link(path, tmp_path / "alias.jsonl")

        with pytest.raises(OSError, match="single-link regular file"):
            McpAuditLog(path=path).start("tool", {}, principal_id="p")

    def test_rejects_a_file_owned_by_someone_else(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(audit, "_owned_by_current_process", lambda _metadata: False)

        with pytest.raises(PermissionError, match="file must be owned by this process user"):
            McpAuditLog(path=tmp_path / "audit.jsonl").start("tool", {}, principal_id="p")
