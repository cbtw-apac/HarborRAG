"""`harborrag source` manages connections through the same service as the API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app_test_connections import ConnectionWorld, connection_world
from app_test_fixtures import MockAppService
from project_test_support import scaffold

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.cli.source_support import ConnectionWriteBuilder, SourceRef
from harborrag_app.workflow_control.control_plane.connections import ConnectionService
from harborrag_core.domain.project import Project

_JIRA = """
source_type: jira
name: CPM tickets
credentials:
  method: api_token
  base_url: https://acme.atlassian.net
  email: bot@acme.com
  api_token: ATATT3xFfGF0-sentinel-9f2c
config:
  jql: project = CPM
advanced:
  page_size: 20
"""


class _SourceApp(MockAppService):
    """Mock app service carrying the real connection service over fakes."""

    def __init__(self, world: ConnectionWorld) -> None:
        super().__init__()
        self.world = world

    @property
    def connection_service(self) -> ConnectionService:
        return self.world.service


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConnectionWorld:
    created = connection_world(
        [Project(id="default", name="Default", collection="default", tenant_id="DEFAULT")]
    )
    monkeypatch.setattr(cli_runner, "runtime_app_service", lambda: _SourceApp(created))
    monkeypatch.chdir(scaffold(tmp_path / "project"))
    return created


@pytest.fixture
def body(tmp_path: Path) -> Path:
    path = tmp_path / "jira.yaml"
    path.write_text(_JIRA, encoding="utf-8")
    return path


def _run(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, dict[str, Any]]:
    capsys.readouterr()
    code = cli.main(["source", *arguments, "--json"])
    out = capsys.readouterr().out.strip()
    return code, json.loads(out) if out else {}


def test_add_show_and_list_a_connection_without_printing_its_secret(
    world: ConnectionWorld, body: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    added, created = _run(capsys, "add", "default/jira-cpm", "--file", str(body))
    shown, connection = _run(capsys, "show", "default/jira-cpm")
    listed, page = _run(capsys, "list", "--project-id", "default")

    assert (added, shown, listed) == (0, 0, 0)
    assert created["data"]["ref"] == "default/jira-cpm"
    assert created["data"]["advanced"] == {"page_size": 20}
    assert connection["data"]["credentials"]["api_token"]["last4"] == "9f2c"
    assert connection["data"]["version"] == 1
    assert [item["ref"] for item in page["data"]["items"]] == ["default/jira-cpm"]
    assert "sentinel" not in json.dumps([created, connection, page])


def test_add_refuses_an_existing_id_and_update_a_missing_one(
    world: ConnectionWorld, body: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(capsys, "add", "default/jira-cpm", "--file", str(body))

    again, refused = _run(capsys, "add", "default/jira-cpm", "--file", str(body))
    missing, not_found = _run(capsys, "update", "default/other", "--file", str(body))

    assert (again, refused["data"]["error_type"]) == (1, "HarborPreconditionFailedError")
    assert (missing, not_found["error"]) == (1, "connection 'other' not found")


def test_update_keeps_omitted_secrets_and_checks_the_version(
    world: ConnectionWorld, body: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(capsys, "add", "default/jira-cpm", "--file", str(body))
    edited = tmp_path / "edited.yaml"
    edited.write_text(
        _JIRA.replace("  api_token: ATATT3xFfGF0-sentinel-9f2c\n", "").replace("CPM", "OPS"),
        encoding="utf-8",
    )

    updated, result = _run(capsys, "update", "default/jira-cpm", "--file", str(edited))
    stale, conflict = _run(
        capsys, "update", "default/jira-cpm", "--file", str(edited), "--if-version", "1"
    )

    assert updated == 0
    assert result["data"]["config"] == {"jql": "project = OPS"}
    assert result["data"]["credentials"]["api_token"]["last4"] == "9f2c"
    assert result["data"]["version"] == 2
    assert (stale, conflict["data"]["error_type"]) == (1, "HarborPreconditionFailedError")


def test_a_validation_error_names_each_field(
    world: ConnectionWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("source_type: confluence\ncredentials: {}\nconfig: {}\n", encoding="utf-8")

    code, response = _run(capsys, "add", "default/wiki", "--file", str(broken))

    fields = {issue["field"] for issue in response["data"]["details"]}
    assert code == 1
    assert {"credentials.base_url", "config.space_key"} <= fields
    assert "config.space_key: required" in response["error"]


def test_a_project_named_by_its_display_name_points_at_its_id(
    world: ConnectionWorld, body: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, response = _run(capsys, "add", "Default/jira-cpm", "--file", str(body))

    assert code == 1
    assert response["error"] == "project 'Default' not found; did you mean 'default'?"


def test_sync_starts_an_ingestion_and_remove_needs_yes_without_a_terminal(
    world: ConnectionWorld, body: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(capsys, "add", "default/jira-cpm", "--file", str(body))

    synced, ingestion = _run(capsys, "sync", "default/jira-cpm", "--mode", "full")
    capsys.readouterr()
    unconfirmed = cli.main(["source", "remove", "default/jira-cpm"])
    message = capsys.readouterr().err
    world.runs.active = None
    removed, job = _run(capsys, "remove", "default/jira-cpm", "--yes")

    assert (synced, ingestion["data"]["mode"]) == (0, "full")
    assert unconfirmed == 2 and "--yes" in message
    assert removed == 0 and job["data"]["job_id"]


def test_a_failed_connection_test_exits_1(
    world: ConnectionWorld, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("source_type: local_file\nconfig:\n  source_path: /nope\n", encoding="utf-8")

    code, response = _run(capsys, "test", "default/files", "--file", str(local))

    assert code == 1
    assert response["ok"] is True and response["data"]["ok"] is False


def test_a_body_file_refuses_unknown_fields_and_a_bad_reference() -> None:
    with pytest.raises(ValueError, match="unknown field sync_now"):
        ConnectionWriteBuilder().from_mapping({"source_type": "jira", "sync_now": True})
    with pytest.raises(ValueError, match="PROJECT/CONNECTION"):
        SourceRef.parse("jira-cpm")

    write = ConnectionWriteBuilder().from_mapping({"source_type": "github"}).build()

    assert (write.credentials, write.config, write.tuning) == ({}, {}, None)


def test_a_terminal_gets_a_table_and_the_next_command(
    world: ConnectionWorld, body: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(capsys, "add", "default/jira-cpm", "--file", str(body))

    assert cli.main(["source", "list"]) == 0
    table = capsys.readouterr().out
    assert cli.main(["source", "sync", "default/jira-cpm"]) == 0
    started = capsys.readouterr().out

    assert "default/jira-cpm" in table and "active" in table
    assert "Follow it: harborrag ingest watch ing_" in started
