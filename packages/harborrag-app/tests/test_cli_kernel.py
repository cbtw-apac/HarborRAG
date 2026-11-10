"""The CLI kernel: global options, profiles, output formats, the error envelope and exit codes."""

from __future__ import annotations

import getpass
import io
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import pytest
import typer
import yaml
from app_test_fixtures import MockAppService
from cli_fakes import FakeOperations, raised_in_temporal_client, serving
from pydantic import BaseModel

from harborrag_app.api.v1.admin.connection_schemas import ProjectOut, ProjectWriteRequest
from harborrag_app.api.v1.me_schemas import MeOut, MePrincipal
from harborrag_app.cli import invoke
from harborrag_app.cli import main as cli
from harborrag_app.cli.exit_codes import exit_code_for
from harborrag_app.cli.globals import (
    ConfigOption,
    Globals,
    OutputOption,
    ProfileOption,
    TenantOption,
    YesOption,
    resolve_globals,
)
from harborrag_app.cli.output import OutputFormat, print_body
from harborrag_app.cli.tenant_guard import confirm_tenant_write
from harborrag_app.cli.views import render
from harborrag_app.operations import Saved
from harborrag_app.workflow_control import AppResponse
from harborrag_core.contracts.errors import HarborConfigurationError, HarborNotFoundError
from harborrag_runtime.config.connectors.models.spec import ConnectionValidationError, FieldIssue

pytestmark = pytest.mark.unit

_ME = MeOut(
    principal=MePrincipal(principal_type="user", principal_id="cli:alex", name="cli:alex"),
    tenant_id="DEFAULT",
    tenants=["*"],
    role="owner",
)
_STAMP = datetime(2026, 11, 10, tzinfo=UTC)
_PROJECT = ProjectOut(
    project_id="harborrag-workspace",
    tenant_id="DEMO",
    project_name="HarborRAG workspace",
    project_description=None,
    connection_count=0,
    document_count=0,
    created_at=_STAMP,
    updated_at=_STAMP,
)
_TRACE = re.compile(r" \(trace [0-9a-f]{8}…\)$")


@pytest.fixture(autouse=True)
def _hermetic(cli_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run inside the project, with no real profile file, environment or stale log stream.

    The CLI's log handler outlives a test, still writing to that test's captured stderr; an
    empty handler list makes ``main()`` attach a fresh one to this test's stream.
    """
    monkeypatch.chdir(cli_project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in (
        "HARBORRAG_TENANT_ID",
        "HARBORRAG_OUTPUT",
        "HARBORRAG_PROFILE",
        "HARBORRAG_LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(logging.getLogger("harborrag"), "handlers", [])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOperations:
    operations = FakeOperations(results={"me": _ME, "put_project": Saved(_PROJECT, "e1", True)})
    monkeypatch.setattr(invoke, "local_operations", serving(operations))
    return operations


def _config(tmp_path: Path, profiles: dict[str, Any]) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"profiles": profiles}), encoding="utf-8")
    return str(path)


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_me_prints_the_body_as_json_for_the_cli_principal(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "me", "-o", "json")

    assert code == 0
    assert MeOut.model_validate_json(out) == _ME
    [call] = fake.calls
    principal = call.args[0]
    assert (principal.subject, principal.role, principal.tenant_ids) == (
        f"cli:{getpass.getuser()}",
        "owner",
        frozenset({"*"}),
    )
    assert call.kwargs == {"tenant_id": "DEFAULT"}


def test_me_prints_json_when_stdout_is_not_a_terminal(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "me")

    assert code == 0
    assert json.loads(out) == _ME.model_dump(mode="json")


def test_output_from_the_environment_beats_the_profile_and_the_flag_beats_both(
    fake: FakeOperations,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, {"default": {"output": "json"}})
    monkeypatch.setenv("HARBORRAG_OUTPUT", "yaml")

    _, from_environment, _ = _run(capsys, "me", "--config", config)
    _, from_flag, _ = _run(capsys, "me", "--config", config, "-o", "table")

    assert not from_environment.startswith("{")
    assert yaml.safe_load(from_environment) == _ME.model_dump(mode="json")
    assert "role: owner" in from_flag
    assert not from_flag.startswith(("{", "principal:"))


def test_a_profile_with_an_api_url_waits_for_remote_mode(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config = _config(tmp_path, {"default": {"api_url": "https://harborrag.example.com"}})

    code, _, err = _run(capsys, "me", "--config", config)

    assert code == 2
    assert "v2.1.0" in err
    assert fake.calls == []


def test_an_unknown_profile_is_a_usage_error(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config = _config(tmp_path, {"default": {"tenant": "DEMO"}})

    in_file, _, _ = _run(capsys, "me", "--config", config, "--profile", "staging")
    without_file, _, _ = _run(capsys, "me", "--profile", "staging")

    assert (in_file, without_file) == (2, 2)
    assert fake.calls == []


def test_a_missing_resource_prints_one_error_line_and_exits_3(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.errors["me"] = HarborNotFoundError("connection default/x not found")

    code, out, err = _run(capsys, "me", "-o", "table")

    assert code == 3
    assert out == ""
    assert err.startswith("error 404 not_found: connection default/x not found (trace ")
    assert _TRACE.search(err.strip())


def test_json_mode_prints_the_http_error_envelope(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.errors["me"] = HarborNotFoundError("connection default/x not found")

    code, _, err = _run(capsys, "me", "-o", "json")
    error = json.loads(err)["error"]

    assert code == 3
    assert error["code"] == "not_found"
    assert error["message"] == "connection default/x not found"
    assert error["details"] is None
    assert re.fullmatch(r"[0-9a-f]{32}", error["trace_id"])


def test_field_issues_follow_the_message_and_exit_2(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    issue = FieldIssue("config.source_path", "must be inside /data/sources")
    fake.errors["me"] = ConnectionValidationError([issue])

    code, _, err = _run(capsys, "me", "-o", "table")

    assert code == 2
    assert err.startswith("error 422 validation_error: ")
    assert _TRACE.sub("", err.strip()).endswith(
        "; config.source_path: must be inside /data/sources"
    )


def test_a_lost_temporal_connection_is_unavailable_and_exits_6(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.errors["me"] = raised_in_temporal_client()

    code, _, err = _run(capsys, "me", "-o", "table")

    assert code == 6
    assert err.startswith("error 503 dependency_unavailable: ")
    assert "Traceback" not in err


probe = typer.Typer()


@probe.command()
def _put_project(
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    resolved = resolve_globals(
        tenant=tenant,
        output=output,
        profile=profile,
        config=config,
        yes=yes,
        environ=os.environ,
        stdout_is_tty=False,
    )
    body = ProjectWriteRequest(project_name="HarborRAG workspace")
    invoke.run_operation(
        "put_project",
        lambda operations, scope: operations.put_project(
            scope, "harborrag-workspace", body, if_match=None, if_none_match=None
        ),
        globals=resolved,
        write=True,
    )


def _probe(*argv: str) -> int:
    try:
        probe(args=list(argv), prog_name="harborrag")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


def test_a_first_write_into_an_empty_tenant_needs_yes_without_a_terminal(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.has_data = False

    code = _probe("--tenant", "DEMO")

    assert code == 2
    assert "Tenant `DEMO` has no data yet; pass --yes to create it." in capsys.readouterr().err
    assert (fake.tenant_checks, fake.calls) == (["DEMO"], [])


@pytest.mark.parametrize(("has_data", "argv"), [(False, ["--yes"]), (True, [])])
def test_a_write_runs_with_yes_or_into_a_tenant_with_data(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], has_data: bool, argv: list[str]
) -> None:
    fake.has_data = has_data

    code = _probe("--tenant", "DEMO", *argv)

    assert code == 0
    assert [call.operation_id for call in fake.calls] == ["put_project"]
    assert json.loads(capsys.readouterr().out)["project_id"] == "harborrag-workspace"


@pytest.mark.asyncio
async def test_declining_the_new_tenant_prompt_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))

    with pytest.raises(click.exceptions.Abort):
        await confirm_tenant_write(
            FakeOperations(has_data=False),  # type: ignore[arg-type]
            "DEMO",
            yes=False,
            interactive=True,
        )


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (200, 0),
        (202, 0),
        (404, 3),
        (409, 4),
        (412, 4),
        (400, 2),
        (413, 2),
        (422, 2),
        (501, 2),
        (401, 5),
        (403, 5),
        (429, 6),
        (503, 6),
        (504, 6),
        (500, 1),
    ],
)
def test_exit_code_for_maps_each_status(status: int, code: int) -> None:
    assert exit_code_for(status) == code


def _resolve(tmp_path: Path, environ: dict[str, str], **flags: Any) -> Globals:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"profiles": {"default": {"tenant": "engineering", "output": "yaml"}}}),
        encoding="utf-8",
    )
    options: dict[str, Any] = {"tenant": None, "output": None, "profile": None, "yes": False}
    return resolve_globals(**(options | flags), config=config, environ=environ, stdout_is_tty=True)


def test_tenant_takes_the_flag_then_the_environment_then_the_profile(tmp_path: Path) -> None:
    environ = {"HARBORRAG_TENANT_ID": "DEMO"}

    assert _resolve(tmp_path, environ, tenant="acme").tenant == "acme"
    assert _resolve(tmp_path, environ).tenant == "DEMO"
    assert _resolve(tmp_path, {}).tenant == "engineering"
    assert _resolve(tmp_path, {}).output is OutputFormat.YAML


def test_tenant_and_output_fall_back_to_the_built_in_defaults() -> None:
    resolved = resolve_globals(
        tenant=None,
        output=None,
        profile=None,
        config=None,
        yes=False,
        environ={},
        stdout_is_tty=True,
    )

    assert (resolved.tenant, resolved.output, resolved.profile) == (
        "DEFAULT",
        OutputFormat.TABLE,
        "default",
    )


def test_an_unknown_output_in_the_environment_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(click.UsageError, match="HARBORRAG_OUTPUT"):
        _resolve(tmp_path, {"HARBORRAG_OUTPUT": "xml"})


def test_table_mode_lists_identifiers_and_the_next_cursor(
    capsys: pytest.CaptureFixture[str],
) -> None:
    page = {"items": [_PROJECT.model_dump(mode="json")], "next_cursor": "c2"}

    print_body("list_projects", page, output=OutputFormat.TABLE)
    out = capsys.readouterr().out

    assert "harborrag-workspace" in out
    assert "HarborRAG workspace" in out
    assert out.strip().endswith("More: --cursor c2")


def test_table_mode_shows_a_stored_secret_by_its_last_four_characters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = {"set": True, "last4": "0001", "updated_at": None, "from": "stored"}

    render("get_connection", {"name": "Engineering Jira", "credentials": {"api_token": secret}})
    out = capsys.readouterr().out

    assert "api_token: set ****0001" in out
    assert "stored" not in out


def test_an_empty_body_prints_done_only_in_table_mode(capsys: pytest.CaptureFixture[str]) -> None:
    print_body("delete_project", None, output=OutputFormat.JSON)
    print_body("delete_project", None, output=OutputFormat.TABLE)

    assert capsys.readouterr().out == "Done.\n"


class _Unready(MockAppService):
    def health(self) -> AppResponse:
        return AppResponse(False, {}, "control-plane migrations failed")


def test_me_runs_the_real_operation_over_the_built_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(invoke, "runtime_app_service", MockAppService)

    code, out, _ = _run(capsys, "me", "--tenant", "DEMO")

    assert code == 0
    assert json.loads(out)["principal"]["principal_id"] == f"cli:{getpass.getuser()}"
    assert json.loads(out)["tenant_id"] == "DEMO"


def test_an_unready_control_plane_is_one_unavailable_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(invoke, "runtime_app_service", _Unready)

    code, _, err = _run(capsys, "me", "-o", "table")

    assert code == 6
    assert err.startswith("error 503 dependency_unavailable: Control plane is not ready")
    assert err.count("\n") == 1


def test_an_internal_error_is_the_only_thing_on_stderr(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.errors["me"] = ValueError("boom")

    code, _, err = _run(capsys, "me", "-o", "json")

    assert code == 1
    assert json.loads(err)["error"]["code"] == "internal_error"


_COMPOSITION = logging.getLogger("harborrag.runtime.composition.control_plane")


def _unready_after_a_runtime_error() -> MockAppService:
    _COMPOSITION.error("Control-plane migrations failed database_scheme=postgresql")
    return _Unready()


def _unconfigured() -> MockAppService:
    _COMPOSITION.error("Control-plane database is ahead of this build")
    raise HarborConfigurationError("HARBORRAG_SECRETS_ENCRYPTION_KEY must be set")


class _DatabaseSettings(BaseModel):
    control_db_port: int


def _invalid_settings() -> MockAppService:
    _DatabaseSettings.model_validate({"control_db_port": "postgresql://u:secret@h/db"})
    raise AssertionError("the settings validated")


def _broken() -> MockAppService:
    raise RuntimeError("composition broke at postgresql://u:secret@h/db")


_INVALID_PORT = (
    "control_db_port: Input should be a valid integer, unable to parse string as an integer"
)


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (
            _unready_after_a_runtime_error,
            (6, "dependency_unavailable", f"{invoke.CONTROL_PLANE_NOT_READY}{invoke.DEBUG_HINT}"),
        ),
        (_unconfigured, (1, "internal_error", "HARBORRAG_SECRETS_ENCRYPTION_KEY must be set")),
        (_invalid_settings, (1, "internal_error", _INVALID_PORT)),
        (_broken, (1, "internal_error", f"Internal server error{invoke.DEBUG_HINT}")),
    ],
)
def test_a_failed_local_setup_prints_one_envelope_that_says_what_to_fix(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    factory: Callable[[], MockAppService],
    expected: tuple[int, str, str],
) -> None:
    monkeypatch.setattr(invoke, "runtime_app_service", factory)

    code, _, err = _run(capsys, "me", "-o", "json")
    error = json.loads(err)["error"]

    assert (code, error["code"], error["message"]) == expected
    assert "secret" not in err


def test_the_renamed_tenant_variable_is_named_in_the_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARBORRAG_INGESTION_TENANT_ID", "acme")

    json_code, _, json_err = _run(capsys, "me", "-o", "json")
    table_code, _, table_err = _run(capsys, "me", "-o", "table")
    error = json.loads(json_err)["error"]

    assert (json_code, table_code) == (1, 1)
    assert "HARBORRAG_INGESTION_TENANT_ID was renamed to HARBORRAG_TENANT_ID" in error["message"]
    assert table_err.startswith("error 500 internal_error: ")
    assert "renamed to HARBORRAG_TENANT_ID" in table_err
    assert table_err.count("\n") == 1


def test_a_run_from_a_project_subdirectory_leaves_stderr_to_the_error(
    fake: FakeOperations,
    capsys: pytest.CaptureFixture[str],
    cli_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (cli_project / "sub").mkdir()
    monkeypatch.chdir(cli_project / "sub")
    fake.errors["me"] = HarborNotFoundError("connection default/x not found")

    code, _, err = _run(capsys, "me", "-o", "json")

    assert code == 3
    assert json.loads(err)["error"]["code"] == "not_found"


def test_debug_logging_shows_the_cause_of_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARBORRAG_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(invoke, "runtime_app_service", _unconfigured)

    code, _, err = _run(capsys, "me", "-o", "table")

    assert code == 1
    assert "Control-plane database is ahead of this build" in err
    assert "Traceback" in err
    assert "HarborConfigurationError: HARBORRAG_SECRETS_ENCRYPTION_KEY must be set" in err
    assert err.splitlines()[-1].startswith("error 500 internal_error: ")


def test_a_success_still_prints_the_warnings_it_logged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def warned() -> MockAppService:
        _COMPOSITION.warning("Control-plane schema check skipped")
        return MockAppService()

    monkeypatch.setattr(invoke, "runtime_app_service", warned)

    code, out, err = _run(capsys, "me", "-o", "json")

    assert code == 0
    assert json.loads(out)["role"] == "owner"
    assert "Control-plane schema check skipped" in err
