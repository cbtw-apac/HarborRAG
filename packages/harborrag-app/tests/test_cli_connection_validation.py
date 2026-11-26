"""``harborrag connections`` refuses a bad connection id or source type with 422, as HTTP does."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import invoke
from harborrag_app.cli import main as cli
from harborrag_app.operations import LocalOperations
from harborrag_core.domain.project import Project
from harborrag_runtime.testing.connection_fakes import connection_world

pytestmark = pytest.mark.unit

_USAGE = 2


class _ConnectionApp(MockAppService):
    """Mock app service with a real connection service over in-memory stores."""

    def __init__(self) -> None:
        super().__init__()
        world = connection_world(
            [Project(id="engineering", name="Engineering", collection="eng", tenant_id="acme")]
        )
        self.connection_service = world.service

    async def tenant_has_data(self, tenant_id: str) -> bool:
        return True


@pytest.fixture(autouse=True)
def _hermetic(cli_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(cli_project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("HARBORRAG_TENANT_ID", "HARBORRAG_OUTPUT", "HARBORRAG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(logging.getLogger("harborrag"), "handlers", [])
    operations = LocalOperations(_ConnectionApp())  # type: ignore[arg-type]  # a mock app service

    @asynccontextmanager
    async def local_operations() -> AsyncIterator[LocalOperations]:
        yield operations

    monkeypatch.setattr(invoke, "local_operations", local_operations)


@pytest.fixture
def body_file(tmp_path: Path) -> Path:
    path = tmp_path / "body.json"
    path.write_text(
        json.dumps(
            {
                "source_type": "jira",
                "name": "Engineering Jira",
                "credentials": {
                    "base_url": "https://example.atlassian.net",
                    "email": "bot@example.com",
                    "api_token": "demo-token-sentinel-0001",
                },
                "config": {"jql": "project = DEMO"},
            }
        ),
        encoding="utf-8",
    )
    return path


def _error(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    capsys.readouterr()
    code = cli.main([*argv, "--tenant", "acme", "-o", "json"])
    err = capsys.readouterr().err
    return code, json.loads(err)["error"]


def _fields(error: dict[str, object]) -> list[object]:
    details = error["details"]
    assert isinstance(details, list)
    return [issue["field"] for issue in details]


@pytest.mark.parametrize("project_id", ["engineering", "missing"])
def test_cli_connections_create_bad_connection_id_exits_2(
    capsys: pytest.CaptureFixture[str], body_file: Path, project_id: str
) -> None:
    code, error = _error(
        capsys, "connections", "create", f"{project_id}/Jira_Main", "-f", str(body_file), "-y"
    )

    assert code == _USAGE
    assert error["code"] == "validation_error"
    assert _fields(error) == ["connection_id"]


@pytest.mark.parametrize(
    "argv",
    [
        ("connections", "get", "engineering/Jira_Main"),
        ("connections", "delete", "engineering/Jira_Main", "-y"),
        ("connections", "test", "engineering/Jira_Main"),
    ],
    ids=["get", "delete", "test"],
)
def test_cli_connections_bad_connection_id_exits_2(
    capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    code, error = _error(capsys, *argv)

    assert code == _USAGE
    assert error["code"] == "validation_error"


def test_cli_connections_list_unknown_source_type_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, error = _error(capsys, "connections", "list", "--source-type", "foo")

    assert code == _USAGE
    assert error["code"] == "validation_error"
    assert _fields(error) == ["source_type"]


def test_cli_connections_list_known_source_type_exits_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    capsys.readouterr()
    code = cli.main(["connections", "list", "--source-type", "jira", "--tenant", "acme"])

    assert code == 0
