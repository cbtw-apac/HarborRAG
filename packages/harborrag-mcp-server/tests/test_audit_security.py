from __future__ import annotations

import os

import pytest

from harborrag_mcp_server.audit import McpAuditLog


def test_audit_memory_is_bounded_and_never_stores_arguments() -> None:
    log = McpAuditLog(max_entries=2)
    for index in range(3):
        log.start("tool", {"secret": f"value-{index}"}, principal_id="subject")

    assert len(log.entries) == 2
    assert all("arguments_sha256" in entry for entry in log.entries)
    assert "value-" not in repr(log.entries)


def test_durable_audit_rejects_symlinks_and_repairs_existing_mode(tmp_path) -> None:
    target = tmp_path / "target.log"
    target.write_text("untouched\n", encoding="utf-8")
    link = tmp_path / "audit-link.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        McpAuditLog(path=link).start("search", {}, principal_id="owner")
    assert target.read_text(encoding="utf-8") == "untouched\n"

    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("", encoding="utf-8")
    os.chmod(audit_path, 0o644)
    McpAuditLog(path=audit_path).start("search", {}, principal_id="owner")
    assert audit_path.stat().st_mode & 0o777 == 0o600


def test_durable_audit_rejects_symlinked_or_shared_parent_directory(tmp_path) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(private_directory, target_is_directory=True)

    with pytest.raises(OSError):
        McpAuditLog(path=linked_directory / "audit.jsonl").start("search", {}, principal_id="owner")
    assert not (private_directory / "audit.jsonl").exists()

    shared_directory = tmp_path / "shared"
    shared_directory.mkdir(mode=0o755)
    os.chmod(shared_directory, 0o755)
    with pytest.raises(PermissionError, match="owner-only"):
        McpAuditLog(path=shared_directory / "audit.jsonl").start("search", {}, principal_id="owner")
    assert shared_directory.stat().st_mode & 0o777 == 0o755
