"""The preflight runs the server's own start-up once and reports a failure in one line."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import harborrag_mcp_server.__main__ as cli
from harborrag_core.ports.api_keys import AuthStoreUnavailable
from harborrag_engine.tools.references import KnowledgeReferenceStore
from harborrag_mcp_server.preflight import describe_startup_failure

pytestmark = pytest.mark.unit

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TOKEN = "local-test-token-that-is-at-least-32-bytes"
_MODEL_VARIABLES = (
    "HARBORRAG_CHAT_MODEL",
    "HARBORRAG_CHAT_API_KEY",
    "HARBORRAG_EMBED_MODEL",
    "HARBORRAG_EMBED_API_KEY",
)


class _Runtime:
    """A reader runtime that records its lifecycle instead of connecting backends."""

    invoker = None
    config_provider = None
    tenant_directory = None

    def __init__(self) -> None:
        self.references = KnowledgeReferenceStore()
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True


class _UnreachableKeyStore:
    async def verify_key(self, raw_key: str) -> None:
        raise AuthStoreUnavailable("MCP key store is unavailable")


@pytest.fixture(autouse=True)
def _launch_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Start every test from an empty directory with the default local auth mode."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARBORRAG_MCP_BEARER_TOKEN", TOKEN)
    for name in ("HARBORRAG_MCP_AUTH_MODE", "HARBORRAG_MCP_KEY_STORE", "HARBORRAG_MCP_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> _Runtime:
    import harborrag_runtime.composition.readers as readers

    fake = _Runtime()
    monkeypatch.setattr(readers, "open_reader_application", lambda settings: fake)
    return fake


def _error_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if line.strip()]


def test_preflight_passes_after_starting_and_stopping_the_runtime(
    runtime: _Runtime, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--http", "--preflight"]) == 0

    captured = capsys.readouterr()
    assert (runtime.started, runtime.closed) == (True, True)
    assert captured.out == ""
    assert "preflight passed" in captured.err


def test_preflight_names_the_model_variable_the_configuration_needs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in _MODEL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "HARBORRAG_MODEL_CONFIG_PATH", str(_REPOSITORY_ROOT / "config/models/models.yaml")
    )

    assert cli.main(["--http", "--preflight"]) == 1

    lines = _error_lines(capsys)
    assert len(lines) == 1
    assert re.search(r"preflight failed: .*environment variable HARBORRAG_\w+ is not set", lines[0])


def test_preflight_reports_an_unreachable_key_store(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import harborrag_runtime.composition.mcp_auth as mcp_auth

    monkeypatch.setattr(
        mcp_auth, "build_api_key_verification", lambda settings: _UnreachableKeyStore()
    )
    monkeypatch.setenv("HARBORRAG_MCP_AUTH_MODE", "api_key")
    monkeypatch.delenv("HARBORRAG_MCP_BEARER_TOKEN")

    assert cli.main(["--http", "--preflight"]) == 1

    lines = _error_lines(capsys)
    assert len(lines) == 1
    assert "AuthStoreUnavailable" in lines[0]
    assert runtime.closed


def test_preflight_reports_a_missing_tool_configuration(
    runtime: _Runtime, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--preflight", "--config", str(tmp_path / "absent.yaml")]) == 1

    lines = _error_lines(capsys)
    assert len(lines) == 1
    assert "MCP configuration does not exist" in lines[0]
    assert not runtime.started


def test_startup_failure_description_leaves_setting_values_out() -> None:
    class _Settings(BaseModel):
        port: int

    with pytest.raises(ValidationError) as caught:
        _Settings(port="not-a-port-value")  # type: ignore[arg-type]  # the invalid input under test

    description = describe_startup_failure(caught.value)

    assert "port" in description
    assert "not-a-port-value" not in description
    assert "\n" not in description


def test_startup_failure_description_keeps_a_multi_line_cause_on_one_line() -> None:
    unreachable_backend = RuntimeError(
        "<AioRpcError of RPC that terminated with:\n"
        "\tstatus = StatusCode.UNAVAILABLE\n"
        '\tdetails = "failed to connect to all addresses"\n'
        ">"
    )

    description = describe_startup_failure(unreachable_backend)

    assert "\n" not in description
    assert "StatusCode.UNAVAILABLE" in description
    assert "failed to connect to all addresses" in description
