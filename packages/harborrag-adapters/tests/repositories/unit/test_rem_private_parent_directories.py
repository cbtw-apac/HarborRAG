"""Local stores create missing parent directories owner-only and keep existing modes."""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from harborrag_adapters.repositories.backends.sqlite import prepare_sqlite_database
from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)
from harborrag_adapters.repositories.tool_audit import JsonlToolExecutionAudit
from harborrag_core.contracts.tools import ToolInvocationContext
from harborrag_core.security import AccessContext

pytestmark = pytest.mark.unit

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@posix_only
@pytest.mark.asyncio
@pytest.mark.usefixtures("permissive_umask")
async def test_tool_audit_creates_missing_parents_owner_only(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    audit = JsonlToolExecutionAudit(shared / ".harborrag" / "audit" / "tool-audit.jsonl")
    context = ToolInvocationContext(
        AccessContext(principal_id="reader", tenant_id="demo"), invocation_id="call-1"
    )

    await audit.record(context, "vector_search", "success")

    assert _mode(shared / ".harborrag") == 0o700
    assert _mode(shared / ".harborrag" / "audit") == 0o700
    assert _mode(shared) == 0o755


@posix_only
@pytest.mark.usefixtures("permissive_umask")
def test_sqlite_creates_missing_parents_owner_only(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)

    prepare_sqlite_database(str(shared / ".harborrag" / "state" / "control.db"))

    assert _mode(shared / ".harborrag") == 0o700
    assert _mode(shared / ".harborrag" / "state") == 0o700
    assert _mode(shared) == 0o755


def test_relative_object_store_root_warns_without_naming_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        FilesystemObjectStore(root=Path("demo-relative-objects"))

    [record] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert "HARBORRAG_OBJECT_STORE_ROOT" in record.getMessage()
    assert "demo-relative-objects" not in f"{record.getMessage()} {record.__dict__!r}"


def test_absolute_object_store_root_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        FilesystemObjectStore(root=tmp_path / "objects")

    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
