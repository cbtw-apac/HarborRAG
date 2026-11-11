"""The CLI and HTTP stay in step: operations, response bodies, error codes and the reference."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path
from typing import Any

import click
import pytest
import typer
from app_test_fixtures import MockAppService
from cli_fakes import FakeOperations, raised_in_temporal_client, serving
from cli_operation_samples import SAMPLES
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.export_openapi import export_openapi
from harborrag_app.api.settings import ApiSettings
from harborrag_app.cli import invoke
from harborrag_app.cli import main as cli
from harborrag_app.cli.commands.legacy import LEGACY
from harborrag_app.cli.exit_codes import exit_code_for
from harborrag_app.cli.reference import render_reference
from harborrag_app.cli.registry import CLI_ONLY, COMMAND_OPERATIONS, HTTP_ONLY
from harborrag_app.operations.errors import ReferenceNotFoundError
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborAuthError,
    HarborCapabilityError,
    HarborNotFoundError,
    HarborPreconditionFailedError,
)
from harborrag_runtime.config.connectors.models.spec import ConnectionValidationError, FieldIssue

pytestmark = pytest.mark.unit

_REFERENCE_PAGE = (
    Path(__file__).resolve().parents[3] / "docs" / "users" / "cli-reference" / "commands.md"
)
_REF = "engineering/jira-main"
_BODY_FILE = "connection.json"
_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "me": (),
    "projects list": (),
    "projects get": ("engineering",),
    "projects create": ("engineering", "--project-name", "Engineering"),
    "projects update": ("engineering", "--project-name", "Engineering"),
    "projects delete": ("engineering", "--cascade", "-y"),
    "source-types list": (),
    "connections list": (),
    "connections get": (_REF,),
    "connections create": (_REF, "-f", _BODY_FILE),
    "connections update": (_REF, "-f", _BODY_FILE),
    "connections delete": (_REF, "-y"),
    "connections test": (_REF,),
    "ingestions start": (_REF,),
    "ingestions list": (),
    "ingestions get": ("ing_1",),
    "ingestions pause": ("ing_1",),
    "ingestions resume": ("ing_1",),
    "ingestions cancel": ("ing_1",),
    "ingestions retry-failures": ("ing_1",),
    "failures list": (),
    "failures retry": ("--ids", "fail_1"),
    "settings schema": (),
    "settings get": (),
    "settings set": ("chunking.max_tokens=256",),
    "chat": ("Who owns DEMO-1?",),
}
_PATH_VALUES = {
    "project_id": "engineering",
    "connection_id": "jira-main",
    "ingestion_id": "ing_1",
    "session_id": "ses_1",
}
_ERRORS = [
    pytest.param(
        lambda: HarborNotFoundError("connection engineering/jira-main not found"),
        (404, "not_found"),
        id="not_found",
    ),
    pytest.param(
        lambda: ReferenceNotFoundError("project engineering not found"),
        (422, "reference_not_found"),
        id="reference_not_found",
    ),
    pytest.param(
        lambda: ConnectionValidationError([FieldIssue(field="config.jql", issue="required")]),
        (422, "validation_error"),
        id="connection_validation",
    ),
    pytest.param(
        lambda: HarborAlreadyRunningError(
            "an ingestion is running", details={"ingestion_id": "ing_1"}
        ),
        (409, "already_running"),
        id="already_running",
    ),
    pytest.param(
        lambda: HarborPreconditionFailedError("the connection changed"),
        (412, "precondition_failed"),
        id="precondition_failed",
    ),
    pytest.param(
        lambda: HarborCapabilityError("syncing selected items is not supported yet"),
        (501, "not_supported"),
        id="capability",
    ),
    pytest.param(
        lambda: HarborAuthError("editor role required", forbidden=True),
        (403, "forbidden"),
        id="forbidden",
    ),
    pytest.param(
        raised_in_temporal_client, (503, "dependency_unavailable"), id="runtime_connection"
    ),
]


@pytest.fixture(autouse=True)
def _hermetic(cli_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(cli_project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("HARBORRAG_TENANT_ID", "HARBORRAG_OUTPUT", "HARBORRAG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HARBORRAG_LOG_LEVEL", raising=False)
    monkeypatch.setattr(logging.getLogger("harborrag"), "handlers", [])
    (cli_project / _BODY_FILE).write_text(
        json.dumps(
            {
                "source_type": "jira",
                "name": "Jira main",
                "credentials": {"base_url": "https://example.atlassian.net"},
                "config": {"jql": "project = DEMO"},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (MockAppService(), "test"))
    app = create_fastapi_app(ApiSettings(env="dev", host="127.0.0.1", auth_mode="none"))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@cache
def _openapi() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(export_openapi())
    return document


def _operations() -> dict[str, dict[str, Any]]:
    """Every operation of the export, by operationId."""
    return {
        operation["operationId"]: operation
        for path_item in _openapi()["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }


def _success_schema(operation_id: str) -> dict[str, Any]:
    """The schema of the operation's first 2xx JSON body."""
    responses = _operations()[operation_id]["responses"]
    schema: dict[str, Any] = next(
        responses[status]["content"]["application/json"]["schema"]
        for status in sorted(responses)
        if status.startswith("2") and "application/json" in responses[status].get("content", {})
    )
    return schema


def _names_no_fields(schema: dict[str, Any]) -> bool:
    """Whether the schema is neither a model reference nor lists properties, so any object fits."""
    return "$ref" not in schema and "properties" not in schema


def _http_body(http: TestClient, operation_id: str) -> object:
    """The body the operation's route answers, with path parameters from ``_PATH_VALUES``."""
    [(path, method)] = [
        (path, method)
        for path, path_item in _openapi()["paths"].items()
        for method, operation in path_item.items()
        if isinstance(operation, dict) and operation.get("operationId") == operation_id
    ]
    response = http.request(method.upper(), path.format(**_PATH_VALUES))
    assert response.is_success, response.text
    return response.json()


def _leaf_commands(command: click.Command, path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Every runnable command under ``command``, hidden ones included, as its path."""
    if isinstance(command, click.Group):
        for name, subcommand in command.commands.items():
            yield from _leaf_commands(subcommand, (*path, name))
    else:
        yield path


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_parity() -> None:
    mapped = set(COMMAND_OPERATIONS.values())
    assert set(_operations()) == mapped | set(HTTP_ONLY)
    assert not mapped & set(HTTP_ONLY)
    assert not set(COMMAND_OPERATIONS) & set(CLI_ONLY)
    classified = {tuple(command.split()) for command in (*COMMAND_OPERATIONS, *CLI_ONLY)}
    assert set(_leaf_commands(typer.main.get_command(cli.app))) == classified | set(LEGACY)


@pytest.mark.parametrize("command", sorted(COMMAND_OPERATIONS))
def test_output_matches_http_schema(
    command: str,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    operation_id = COMMAND_OPERATIONS[command]
    fake = FakeOperations(results=SAMPLES)
    monkeypatch.setattr(invoke, "local_operations", serving(fake))
    schema = _success_schema(operation_id)

    code, out, err = _run(capsys, *command.split(), *_ARGUMENTS[command], "-o", "json")

    assert code == 0, err
    assert fake.calls[-1].operation_id == operation_id
    body = json.loads(out)
    Draft202012Validator({**schema, "components": _openapi()["components"]}).validate(body)
    if _names_no_fields(schema):
        http: TestClient = request.getfixturevalue("http")
        assert isinstance(http.app, FastAPI)
        http.app.state.operations = fake
        assert body == _http_body(http, operation_id)


@pytest.mark.parametrize(("make_error", "expected"), _ERRORS)
def test_errors_match_http(
    make_error: Callable[[], BaseException],
    expected: tuple[int, str],
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeOperations(errors={"start_ingestion": make_error()})
    assert isinstance(http.app, FastAPI)
    http.app.state.operations = fake
    monkeypatch.setattr(invoke, "local_operations", serving(fake))

    response = http.post(
        "/v1/admin/ingestions", json={"project_id": "engineering", "connection_id": "jira-main"}
    )
    fake.errors["start_ingestion"] = make_error()
    code, out, err = _run(capsys, "ingestions", "start", _REF, "-o", "json")

    assert [call.operation_id for call in fake.calls] == ["start_ingestion", "start_ingestion"]
    status, error_code = expected
    assert (response.status_code, response.json()["error"]["code"]) == expected
    assert out == ""
    assert json.loads(err)["error"]["code"] == error_code
    assert code == exit_code_for(status) != 0


def test_reference_is_current() -> None:
    assert render_reference(_openapi()) == _REFERENCE_PAGE.read_text(encoding="utf-8"), (
        "the command reference is stale; run make cli-reference"
    )


def test_reference_refuses_an_operation_missing_from_openapi() -> None:
    with pytest.raises(ValueError, match="is not in the OpenAPI document"):
        render_reference({"paths": {}})
