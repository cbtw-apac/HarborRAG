"""``harborrag me`` acts in the same tenant as every other command."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from cli_fakes import FakeOperations, serving
from cli_operation_samples import SAMPLES

from harborrag_app.cli import invoke
from harborrag_app.cli import main as cli
from harborrag_app.operations.schemas.connections import ProjectPage

pytestmark = pytest.mark.unit


@pytest.fixture
def fake(cli_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeOperations:
    stack_file = tmp_path / "stack.env"
    stack_file.write_text("HARBORRAG_TENANT_ID=ACME\n", encoding="utf-8")
    monkeypatch.setenv("API_ENV_FILE", str(stack_file))
    monkeypatch.chdir(cli_project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("HARBORRAG_TENANT_ID", "HARBORRAG_OUTPUT", "HARBORRAG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(logging.getLogger("harborrag"), "handlers", [])
    operations = FakeOperations(
        results={"me": SAMPLES["me"], "list_projects": ProjectPage(items=[], next_cursor=None)}
    )
    monkeypatch.setattr(invoke, "local_operations", serving(operations))
    return operations


def test_cli_me_without_a_flag_uses_the_stack_tenant(fake: FakeOperations) -> None:
    assert cli.main(["me", "-o", "json"]) == 0
    assert cli.main(["projects", "list", "-o", "json"]) == 0

    me, projects = fake.calls
    assert me.kwargs["tenant_id"] == "ACME"
    assert projects.args[0].tenant_id == "ACME"  # type: ignore[attr-defined]  # the call's Scope
