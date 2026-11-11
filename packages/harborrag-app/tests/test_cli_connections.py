"""``harborrag connections``: refs, body files, secret prompts, ETags, paging and deletion."""

from __future__ import annotations

import getpass
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cli_fakes import Call, FakeOperations, serving

from harborrag_app.api.v1.admin.connection_schemas import (
    ConnectionOut,
    ConnectionPage,
    ConnectionStatsOut,
    ConnectionTestOut,
    ConnectionWriteRequest,
    JobAcceptedOut,
    PreviewOut,
)
from harborrag_app.cli import invoke
from harborrag_app.cli import main as cli
from harborrag_app.operations import ConnectionRef, Saved, Tagged

pytestmark = pytest.mark.unit

_STAMP = datetime(2026, 11, 10, tzinfo=UTC)
_ETAG = 'W/"rev-3"'
_PROMPTED = "prompted-sentinel-0001"


def _connection() -> ConnectionOut:
    return ConnectionOut(
        tenant_id="DEFAULT",
        project_id="default",
        connection_id="demo",
        source_type="jira",
        connection_ref="default/demo",
        name="Demo",
        credentials={"base_url": "https://example.atlassian.net"},
        config={"jql": "project = DEMO"},
        advanced={},
        status="active",
        last_ingestion=None,
        stats=ConnectionStatsOut(documents=None, chunks=None, entities=None, errors=None),
        created_by="cli:alex",
        updated_by="cli:alex",
        created_at=_STAMP,
        updated_at=_STAMP,
    )


class _PagedConnections(FakeOperations):
    """Answers each ``list_connections`` call with the next page."""

    def __init__(self, pages: list[ConnectionPage]) -> None:
        super().__init__()
        self.pages = list(pages)

    async def list_connections(self, *args: object, **kwargs: object) -> ConnectionPage:  # type: ignore[override]  # the canned-result method is generic
        self.calls.append(Call("list_connections", args, dict(kwargs)))
        return self.pages.pop(0)


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
            "get_connection": Tagged(_connection(), _ETAG),
            "put_connection": Saved(_connection(), _ETAG, True),
            "delete_connection": JobAcceptedOut(job_id="job-1"),
            "test_connection": ConnectionTestOut(
                ok=False,
                checks=[],
                error_category="forbidden",
                preview=PreviewOut(estimated_items=None, sample=[]),
                latency_ms=12,
            ),
        }
    )
    monkeypatch.setattr(invoke, "local_operations", serving(operations))
    return operations


@pytest.fixture
def body_file(tmp_path: Path) -> Path:
    path = tmp_path / "body.json"
    path.write_text(
        json.dumps(
            {
                "source_type": "jira",
                "name": "Demo",
                "credentials": {"base_url": "https://example.atlassian.net"},
                "config": {"jql": "project = DEMO"},
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _written(call: Call) -> ConnectionWriteRequest:
    body = call.args[2]
    assert isinstance(body, ConnectionWriteRequest)
    return body


@pytest.mark.parametrize("ref", ["harborrag-workspace", "a/b/c", "/demo", "default/"])
def test_a_malformed_ref_is_a_usage_error_naming_the_form(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], ref: str
) -> None:
    code, _, err = _run(capsys, "connections", "get", ref)

    assert code == 2
    assert "PROJECT_ID/CONNECTION_ID" in err
    assert "harborrag connections list" in err
    assert fake.calls == []


def test_connections_get_reads_the_ref_and_prints_the_body(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "connections", "get", "default/demo", "-o", "json")

    assert code == 0
    assert json.loads(out)["connection_ref"] == "default/demo"
    assert fake.calls[0].args[1] == ConnectionRef("default", "demo")


def test_create_asks_for_a_file(fake: FakeOperations, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = _run(capsys, "connections", "create", "default/demo")

    assert code == 2
    assert fake.calls == []


def test_create_sends_the_file_as_a_create_only_operator_write(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], body_file: Path
) -> None:
    code, _, _ = _run(capsys, "connections", "create", "default/demo", "-f", str(body_file))

    assert code == 0
    [call] = fake.calls
    assert _written(call).source_type == "jira"
    assert call.kwargs == {"if_match": None, "if_none_match": "*", "origin": "operator"}


def test_a_secret_set_by_the_prompt_and_in_the_file_is_a_usage_error(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], body_file: Path
) -> None:
    document = json.loads(body_file.read_text(encoding="utf-8"))
    document["credentials"]["api_token"] = "from-the-file"
    body_file.write_text(json.dumps(document), encoding="utf-8")

    code, _, err = _run(
        capsys,
        "connections",
        "create",
        "default/demo",
        "-f",
        str(body_file),
        "--secret-prompt",
        "api_token",
    )

    assert code == 2
    assert "credentials.api_token is set both by --secret-prompt and in" in err
    assert "from-the-file" not in err
    assert fake.calls == []


def test_a_prompted_secret_reaches_the_operation_and_is_never_printed(
    fake: FakeOperations,
    capsys: pytest.CaptureFixture[str],
    body_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": _PROMPTED)

    code, out, err = _run(
        capsys,
        "connections",
        "create",
        "default/demo",
        "-f",
        str(body_file),
        "--secret-prompt",
        "api_token",
    )

    assert code == 0
    assert _written(fake.calls[0]).credentials["api_token"] == _PROMPTED
    assert fake.calls[0].kwargs["origin"] == "operator"
    assert _PROMPTED not in out + err


def test_a_secret_prompt_without_a_terminal_is_a_usage_error(
    fake: FakeOperations,
    capsys: pytest.CaptureFixture[str],
    body_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    code, _, err = _run(
        capsys,
        "connections",
        "create",
        "default/demo",
        "-f",
        str(body_file),
        "--secret-prompt",
        "api_token",
    )

    assert code == 2
    assert "terminal" in err
    assert fake.calls == []


def test_a_file_without_credentials_is_refused_as_over_http(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "body.json"
    path.write_text(json.dumps({"source_type": "jira", "config": {}}), encoding="utf-8")

    code, _, err = _run(capsys, "connections", "create", "default/demo", "-f", str(path))

    assert code == 2
    assert "credentials" in err
    assert fake.calls == []


def test_an_invalid_body_names_each_missing_field(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "body.json"
    path.write_text("{}", encoding="utf-8")

    code, _, err = _run(capsys, "connections", "create", "default/demo", "-f", str(path))

    assert code == 2
    assert "source_type" in err
    assert "config" in err


def test_update_sends_the_etag_the_read_returned(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], body_file: Path
) -> None:
    code, _, _ = _run(capsys, "connections", "update", "default/demo", "-f", str(body_file))

    assert code == 0
    read, write = fake.calls
    assert (read.operation_id, write.operation_id) == ("get_connection", "put_connection")
    assert write.kwargs == {"if_match": _ETAG, "if_none_match": None, "origin": "operator"}


def test_update_with_if_match_skips_the_read(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], body_file: Path
) -> None:
    code, _, _ = _run(
        capsys,
        "connections",
        "update",
        "default/demo",
        "-f",
        str(body_file),
        "--if-match",
        'W/"rev-3"',
    )

    assert code == 0
    assert [call.operation_id for call in fake.calls] == ["put_connection"]
    assert fake.calls[0].kwargs["if_match"] == 'W/"rev-3"'


def test_a_failed_check_still_exits_zero(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "connections", "test", "default/demo", "-o", "json")

    assert code == 0
    assert json.loads(out)["ok"] is False
    [call] = fake.calls
    assert call.args[2] is None
    assert call.kwargs == {"origin": "operator"}


def test_test_checks_the_values_of_a_file(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], body_file: Path
) -> None:
    code, _, _ = _run(capsys, "connections", "test", "default/demo", "-f", str(body_file))

    assert code == 0
    assert fake.calls[0].args[2].source_type == "jira"


def test_list_all_follows_the_cursor_and_prints_one_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = _connection(), _connection().model_copy(update={"connection_id": "other"})
    operations = _PagedConnections(
        [
            ConnectionPage(items=[first], next_cursor="c1"),
            ConnectionPage(items=[second], next_cursor=None),
        ]
    )
    monkeypatch.setattr(invoke, "local_operations", serving(operations))

    code, out, _ = _run(capsys, "connections", "list", "--all", "--status", "error", "-o", "json")

    body = json.loads(out)
    assert code == 0
    assert [item["connection_id"] for item in body["items"]] == ["demo", "other"]
    assert body["next_cursor"] is None
    assert [call.kwargs["cursor"] for call in operations.calls] == [None, "c1"]
    assert operations.calls[0].kwargs["status"] == "error"


def test_delete_without_a_terminal_or_yes_is_a_usage_error(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    code, _, err = _run(capsys, "connections", "delete", "default/demo")

    assert code == 2
    assert "--yes" in err
    assert fake.calls == []


def test_delete_with_yes_prints_the_job(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        capsys, "connections", "delete", "default/demo", "--purge", "-y", "-o", "json"
    )

    assert (code, json.loads(out)) == (0, {"job_id": "job-1"})
    assert fake.calls[0].kwargs == {"purge": True}
