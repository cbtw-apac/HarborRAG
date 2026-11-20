"""The HTTP server answers only requests addressed to a host name it was told to serve."""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

import harborrag_mcp_server.__main__ as cli
from harborrag_mcp_server.server.http import (
    parse_allowed_hosts,
    require_acknowledged_bind,
    served_origins,
    validate_http_bind,
)

pytestmark = pytest.mark.blackbox

TOKEN = "local-test-token-that-is-at-least-32-bytes"
_STARTUP_TIMEOUT_SECONDS = 60.0
_LAUNCH_VARIABLES = (
    "HARBORRAG_MCP_AUTH_MODE",
    "HARBORRAG_MCP_ALLOWED_HOSTS",
    "HARBORRAG_MCP_KEY_STORE",
    "HARBORRAG_MCP_KEYS_PATH",
    "HARBORRAG_MCP_ALLOW_INSECURE_BIND",
    "HARBORRAG_ENV",
)
_READER_KEY_HASH = hashlib.sha256(b"reader-key-" + b"a" * 40).hexdigest()

_SERVE_WITHOUT_BACKENDS = """
import sys

import harborrag_runtime.composition.readers as readers
from harborrag_engine.tools.references import KnowledgeReferenceStore


class Runtime:
    invoker = None
    config_provider = None
    tenant_directory = None
    references = KnowledgeReferenceStore()

    async def start(self):
        pass

    async def aclose(self):
        pass


readers.open_reader_application = lambda settings: Runtime()
from harborrag_mcp_server.__main__ import main

raise SystemExit(main(["--http", *sys.argv[1:]]))
"""


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until_healthy(base_url: str, server: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise AssertionError(
                f"server exited early: {server.stderr.read() if server.stderr else ''}"
            )
        try:
            if httpx.get(f"{base_url}/healthz", timeout=1.0).status_code == httpx.codes.OK:
                return
        except httpx.TransportError:
            time.sleep(0.1)
    raise AssertionError("server did not become healthy in time")


@contextmanager
def _serving(tmp_path: Path, *arguments: str, **settings: str) -> Iterator[int]:
    """Run the real entry point without backends and yield the port it listens on."""

    port = _free_loopback_port()
    inherited = {name: value for name, value in os.environ.items() if name not in _LAUNCH_VARIABLES}
    server = subprocess.Popen(  # noqa: S603 - the interpreter running these tests, on a fixed script
        [sys.executable, "-c", _SERVE_WITHOUT_BACKENDS, "--port", str(port), *arguments],
        cwd=tmp_path,
        env={
            **inherited,
            "HARBORRAG_MCP_BEARER_TOKEN": TOKEN,
            "HARBORRAG_MCP_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
            **settings,
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_until_healthy(f"http://127.0.0.1:{port}", server)
        yield port
    finally:
        server.terminate()
        server.wait(timeout=30)


@pytest.fixture(autouse=True)
def _default_launch_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARBORRAG_MCP_BEARER_TOKEN", TOKEN)
    for name in _LAUNCH_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_http_server_refuses_requests_addressed_to_another_host(tmp_path: Path) -> None:
    with _serving(tmp_path) as port:
        health = f"http://127.0.0.1:{port}/healthz"
        by_address = httpx.get(health)
        by_localhost_name = httpx.get(health, headers={"Host": f"localhost:{port}"})
        by_other_host = httpx.get(health, headers={"Host": "attacker.example"})
        from_other_origin = httpx.get(health, headers={"Origin": "http://attacker.example"})

    assert by_address.status_code == httpx.codes.OK
    assert by_localhost_name.status_code == httpx.codes.OK
    assert by_other_host.status_code == httpx.codes.MISDIRECTED_REQUEST
    assert from_other_origin.status_code == httpx.codes.FORBIDDEN


def test_server_bound_to_every_interface_still_answers_only_its_named_hosts(
    tmp_path: Path,
) -> None:
    with _serving(
        tmp_path,
        "--host",
        "0.0.0.0",
        HARBORRAG_MCP_ALLOWED_HOSTS="127.0.0.1,localhost",
        HARBORRAG_MCP_ALLOW_INSECURE_BIND="true",
    ) as port:
        health = f"http://127.0.0.1:{port}/healthz"
        by_published_address = httpx.get(health)
        by_service_name = httpx.get(health, headers={"Host": f"mcp:{port}"})
        by_other_host = httpx.get(health, headers={"Host": "attacker.example"})

    assert by_published_address.status_code == httpx.codes.OK
    assert by_service_name.status_code == httpx.codes.MISDIRECTED_REQUEST
    assert by_other_host.status_code == httpx.codes.MISDIRECTED_REQUEST


def test_binding_outside_loopback_needs_named_hosts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError, match="HARBORRAG_MCP_ALLOWED_HOSTS"):
        validate_http_bind(host="0.0.0.0", port=8010, path="/mcp")
    validate_http_bind(host="0.0.0.0", port=8010, path="/mcp", allowed_hosts=("127.0.0.1",))

    monkeypatch.setenv("HARBORRAG_MCP_ALLOW_INSECURE_BIND", "true")
    with pytest.raises(SystemExit, match="2"):
        cli.main(["--http", "--host", "0.0.0.0", "--preflight"])

    assert "HARBORRAG_MCP_ALLOWED_HOSTS" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("environment", "acknowledgement"),
    [("dev", None), ("dev", "false"), ("staging", "true"), ("prod", "true")],
)
def test_bind_beyond_loopback_needs_a_development_acknowledgement(
    environment: str, acknowledgement: str | None
) -> None:
    with pytest.raises(ValueError, match="HARBORRAG_MCP_ALLOW_INSECURE_BIND"):
        require_acknowledged_bind(
            "0.0.0.0", environment=environment, acknowledgement=acknowledgement
        )


def test_acknowledged_development_bind_and_every_loopback_bind_are_accepted() -> None:
    require_acknowledged_bind("0.0.0.0", environment="dev", acknowledgement=" True ")
    require_acknowledged_bind("127.0.0.1", environment="prod", acknowledgement=None)


def test_named_loopback_hosts_alone_do_not_open_a_network_bind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARBORRAG_MCP_ALLOWED_HOSTS", "localhost")

    with pytest.raises(SystemExit, match="2"):
        cli.main(["--http", "--host", "0.0.0.0", "--preflight"])

    assert "HARBORRAG_MCP_ALLOW_INSECURE_BIND" in capsys.readouterr().err


def test_static_owner_token_is_served_only_under_loopback_host_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARBORRAG_MCP_ALLOWED_HOSTS", "127.0.0.1,mcp.example.internal")

    with pytest.raises(SystemExit, match="2"):
        cli.main(["--http", "--preflight"])

    error = capsys.readouterr().err
    assert "mcp.example.internal" in error
    assert "api_key" in error


def _reader_key_file(tmp_path: Path) -> Path:
    path = tmp_path / "keys.yaml"
    path.write_text(
        "version: 1\nkeys:\n"
        "  - key_id: engineering\n"
        "    principal_id: mcp-reader:engineering\n"
        "    tenant_id: DEFAULT\n"
        "    secret_hash_env: TEST_MCP_KEY_HASH\n",
        encoding="utf-8",
    )
    return path


def test_key_authenticated_server_answers_a_named_remote_host(tmp_path: Path) -> None:
    with _serving(
        tmp_path,
        HARBORRAG_MCP_AUTH_MODE="api_key",
        HARBORRAG_MCP_KEY_STORE="file",
        HARBORRAG_MCP_KEYS_PATH=str(_reader_key_file(tmp_path)),
        TEST_MCP_KEY_HASH=_READER_KEY_HASH,
        HARBORRAG_MCP_ALLOWED_HOSTS="mcp.example.internal",
    ) as port:
        health = f"http://127.0.0.1:{port}/healthz"
        proxied = {"Host": "mcp.example.internal"}
        by_proxy_name = httpx.get(health, headers=proxied)
        by_other_host = httpx.get(health, headers={"Host": "attacker.example"})
        from_its_https_page = httpx.get(
            health, headers={**proxied, "Origin": "https://mcp.example.internal"}
        )
        from_another_site = httpx.get(
            health, headers={**proxied, "Origin": "https://attacker.example"}
        )

    assert by_proxy_name.status_code == httpx.codes.OK
    assert by_other_host.status_code == httpx.codes.MISDIRECTED_REQUEST
    assert from_its_https_page.status_code == httpx.codes.OK
    assert from_another_site.status_code == httpx.codes.FORBIDDEN


def test_served_origins_cover_both_schemes_of_each_named_remote_host() -> None:
    assert served_origins(("127.0.0.1", "mcp.example.internal", "edge.example.internal:8443")) == (
        "http://mcp.example.internal:*",
        "https://mcp.example.internal:*",
        "http://edge.example.internal:8443",
        "https://edge.example.internal:8443",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ()),
        ("", ()),
        (" 127.0.0.1 , localhost ,", ("127.0.0.1", "localhost")),
        ("mcp.example.internal", ("mcp.example.internal",)),
    ],
)
def test_allowed_hosts_setting_is_a_comma_separated_list(
    value: str | None, expected: tuple[str, ...]
) -> None:
    assert parse_allowed_hosts(value) == expected
