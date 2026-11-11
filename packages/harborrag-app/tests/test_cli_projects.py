"""``harborrag projects`` and ``source-types``: flags, body files, paging and deletion."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cli_fakes import Call, FakeOperations, serving

from harborrag_app.api.v1.admin.connection_schemas import (
    JobAcceptedOut,
    ProjectOut,
    ProjectPage,
    ProjectWriteRequest,
    SourceTypePage,
)
from harborrag_app.cli import invoke
from harborrag_app.cli import main as cli
from harborrag_app.cli.output import OutputFormat, print_body
from harborrag_app.operations import Saved, Tagged
from harborrag_app.operations.contract import dump_body

pytestmark = pytest.mark.unit

_STAMP = datetime(2026, 11, 10, tzinfo=UTC)
_PROJECT = ProjectOut(
    project_id="demo",
    tenant_id="DEFAULT",
    project_name="Demo",
    project_description="Demo tickets",
    connection_count=0,
    document_count=0,
    created_at=_STAMP,
    updated_at=_STAMP,
)
_ETAG = 'W/"rev-3"'


@pytest.fixture(autouse=True)
def _hermetic(cli_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(cli_project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("HARBORRAG_TENANT_ID", "HARBORRAG_OUTPUT", "HARBORRAG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HARBORRAG_LOG_LEVEL", raising=False)
    monkeypatch.setattr(logging.getLogger("harborrag"), "handlers", [])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOperations:
    operations = FakeOperations(
        results={
            "get_project": Tagged(_PROJECT, _ETAG),
            "put_project": Saved(_PROJECT, _ETAG, True),
            "list_projects": ProjectPage(items=[_PROJECT], next_cursor=None),
            "delete_project": None,
            "list_source_types": SourceTypePage(items=[]),
        }
    )
    monkeypatch.setattr(invoke, "local_operations", serving(operations))
    return operations


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _body(call: Call) -> ProjectWriteRequest:
    body = call.args[2]
    assert isinstance(body, ProjectWriteRequest)
    return body


def test_projects_create_sends_the_flags_as_a_create_only_write(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        capsys, "projects", "create", "demo", "--project-name", "Demo", "-o", "json", "-y"
    )

    assert code == 0
    assert json.loads(out)["project_id"] == "demo"
    [call] = fake.calls
    assert call.args[1] == "demo"
    assert _body(call) == ProjectWriteRequest(project_name="Demo")
    assert call.kwargs == {"if_match": None, "if_none_match": "*"}


def test_projects_create_takes_the_body_from_a_file(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "body.json"
    path.write_text(json.dumps({"project_name": "Demo"}), encoding="utf-8")

    code, _, _ = _run(
        capsys, "projects", "create", "demo", "-f", str(path), "--project-description", "Tickets"
    )

    assert code == 0
    assert _body(fake.calls[0]) == ProjectWriteRequest(
        project_name="Demo", project_description="Tickets"
    )


def test_a_field_set_by_a_flag_and_in_the_file_is_a_usage_error(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "body.json"
    path.write_text(json.dumps({"project_name": "Other"}), encoding="utf-8")

    code, _, err = _run(
        capsys, "projects", "create", "demo", "--project-name", "Demo", "-f", str(path)
    )

    assert code == 2
    assert "project_name is set both by --project-name" in err
    assert fake.calls == []


def test_projects_create_without_a_name_names_the_missing_field(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "projects", "create", "demo")

    assert code == 2
    assert "project_name" in err
    assert fake.calls == []


def test_projects_update_replaces_the_body_and_merges_nothing_from_the_stored_project(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "body.json"
    path.write_text(json.dumps({"project_name": "Renamed"}), encoding="utf-8")

    code, _, _ = _run(capsys, "projects", "update", "demo", "-f", str(path))

    assert code == 0
    read, write = fake.calls
    assert (read.operation_id, write.operation_id) == ("get_project", "put_project")
    assert _body(write) == ProjectWriteRequest(project_name="Renamed")
    assert _body(write).project_description is None
    assert write.kwargs == {"if_match": _ETAG, "if_none_match": None}


def test_projects_update_without_a_name_fails_the_request_model(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "projects", "update", "demo", "--project-description", "x")

    assert code == 2
    assert "project_name" in err
    assert fake.calls == []


def test_projects_update_with_if_match_skips_the_read(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(capsys, "projects", "update", "demo", "--project-name", "Renamed", "--if-match", 'W/"x"')

    assert [call.operation_id for call in fake.calls] == ["put_project"]
    assert fake.calls[0].kwargs["if_match"] == 'W/"x"'


def test_projects_list_passes_the_filters(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "projects", "list", "--q", "dem", "--limit", "5", "-o", "json")

    assert code == 0
    assert json.loads(out)["items"][0]["project_id"] == "demo"
    assert fake.calls[0].kwargs == {"q": "dem", "limit": 5, "cursor": None}


def test_projects_get_prints_the_project(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "projects", "get", "demo", "-o", "json")

    assert code == 0
    assert json.loads(out)["project_name"] == "Demo"


def test_deleting_without_a_terminal_or_yes_is_a_usage_error(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    code, _, err = _run(capsys, "projects", "delete", "demo")

    assert code == 2
    assert "--yes" in err
    assert fake.calls == []


def test_a_cascade_delete_prints_the_job_and_an_empty_delete_prints_nothing(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.results["delete_project"] = JobAcceptedOut(job_id="job-1")
    cascaded = _run(capsys, "projects", "delete", "demo", "--cascade", "-y", "-o", "json")
    fake.results["delete_project"] = None
    emptied = _run(capsys, "projects", "delete", "demo", "-y", "-o", "json")

    assert (cascaded[0], json.loads(cascaded[1])) == (0, {"job_id": "job-1"})
    assert (emptied[0], emptied[1]) == (0, "")
    assert [call.kwargs["cascade"] for call in fake.calls] == [True, False]


def test_dump_body_prints_a_model_of_an_operation_without_a_response_model() -> None:
    assert dump_body("delete_project", JobAcceptedOut(job_id="job-1")) == {"job_id": "job-1"}


def test_source_types_list_passes_the_category(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "source-types", "list", "--category", "files", "-o", "json")

    assert code == 0
    assert json.loads(out) == {"items": []}
    assert fake.calls[0].kwargs == {"category": "files"}


def test_source_types_list_refuses_an_unknown_category(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = _run(capsys, "source-types", "list", "--category", "chat")

    assert code == 2
    assert fake.calls == []


def test_a_failure_while_printing_a_committed_write_is_not_reported_as_the_write_failing(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(operation_id: str, body: object, *, output: OutputFormat) -> None:
        raise ValueError("cannot serialize")

    monkeypatch.setattr(invoke, "print_body", broken)

    code, out, err = _run(capsys, "projects", "create", "demo", "--project-name", "Demo", "-y")

    assert code == 1
    assert out == ""
    assert "operation succeeded" in err
    assert "its result could not be printed" in err
    assert [call.operation_id for call in fake.calls] == ["put_project"]


def test_print_body_still_prints_a_job(capsys: pytest.CaptureFixture[str]) -> None:
    print_body("delete_project", JobAcceptedOut(job_id="job-1"), output=OutputFormat.JSON)

    assert json.loads(capsys.readouterr().out) == {"job_id": "job-1"}
