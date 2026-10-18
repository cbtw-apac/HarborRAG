"""With an access file, an unlisted principal is refused before any tool runs."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from harborrag_engine.tools.dispatcher import ToolInvoker
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.configuration import McpConfiguration, McpConfigurationStore
from harborrag_mcp_server.server import (
    McpServer,
    _request_context,
    _tool_handler,
    create_mcp_server,
)
from harborrag_mcp_server.server.http import create_local_token_verifier, register_http_routes

TOKEN = "local-test-token-that-is-at-least-32-bytes"
LOCAL_OWNER = "harborrag-local"
REFUSAL = "not listed in the access file"


class _Listing:
    """The slice of ``AccessFileWatcher`` the transport reads."""

    def __init__(self, listed: dict[str, set[str]]) -> None:
        self._listed = {principal: frozenset(tenants) for principal, tenants in listed.items()}
        self.threads: list[int] = []

    def listed_tenants(self, principal_id: str) -> frozenset[str]:
        self.threads.append(threading.get_ident())
        return self._listed.get(principal_id, frozenset())


class _RecordingInvoker(ToolInvoker):
    calls: list[str]

    async def invoke(
        self, name: str, arguments: dict[str, object], **_: object
    ) -> dict[str, object]:
        self.calls.append(name)
        return {"ok": True, "versions": {}}


def _server(tmp_path: Path, listed: dict[str, set[str]] | None) -> McpServer:
    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    plain = McpServer(audit=audit)
    invoker = _RecordingInvoker(plain.tools or [])
    invoker.calls = []
    server = McpServer(tools=plain.tools, invoker=invoker, audit=audit)
    if listed is not None:
        server.access_watcher = _Listing(listed)
    return server


def test_the_launcher_builds_a_watcher_only_when_an_access_file_is_set(tmp_path: Path) -> None:
    from harborrag_mcp_server.__main__ import _access_watcher
    from harborrag_runtime.config.settings import RuntimeSettings

    catalog = tmp_path / "connectors.yaml"
    catalog.write_text("version: 1\nconnectors:\n  docs:\n    provider: local\n", encoding="utf-8")
    access = tmp_path / "access.yaml"
    access.write_text(
        "version: 1\ntenants:\n  acme:\n    projects:\n      beta:\n"
        "        datasources: [docs]\n        members:\n          - principal: alice\n",
        encoding="utf-8",
    )
    recall = {"memory_recall_scopes": "user,session"}

    off = RuntimeSettings(connector_config_path=catalog, **recall)
    on = RuntimeSettings(access_config_path=access, connector_config_path=catalog, **recall)

    assert _access_watcher(off) is None
    watcher = _access_watcher(on)
    assert watcher is not None
    assert watcher.listed_tenants("alice") == frozenset({"acme"})
    assert watcher.listed_tenants("stranger") == frozenset()


def _calls(server: McpServer) -> list[str]:
    invoker = server.invoker
    assert isinstance(invoker, _RecordingInvoker)
    return invoker.calls


def _as_token(monkeypatch: pytest.MonkeyPatch, token: object | None) -> None:
    dependencies = pytest.importorskip("fastmcp.server.dependencies")
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)


def _reader(subject: str, *tenants: str) -> SimpleNamespace:
    claims = {"sub": subject, "role": "reader", "tenants": list(tenants)}
    return SimpleNamespace(claims=claims, client_id="client")


def test_request_context_refuses_a_principal_the_file_does_not_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_token(monkeypatch, _reader("stranger", "demo"))

    with pytest.raises(PermissionError, match=REFUSAL):
        _request_context("demo", "source_acl", None, _Listing({"someone-else": {"demo"}}))


def test_request_context_refuses_a_tenant_the_principal_is_not_listed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_token(monkeypatch, _reader("reader-1", "*"))
    listing = _Listing({"reader-1": {"demo"}})

    assert str(_request_context("demo", "source_acl", None, listing).access.tenant_id) == "demo"
    with pytest.raises(PermissionError, match=REFUSAL):
        _request_context("other", "source_acl", None, listing)


def test_request_context_checks_the_tenant_a_single_grant_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_token(monkeypatch, _reader("reader-1", "demo"))

    with pytest.raises(PermissionError, match=REFUSAL):
        _request_context(None, "source_acl", None, _Listing({"reader-1": {"other"}}))


def test_request_context_without_a_watcher_checks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_token(monkeypatch, _reader("stranger", "demo"))

    context = _request_context("demo", "source_acl", None)

    assert context.access.principal_id == "stranger"


def test_a_local_stdio_call_is_refused_unless_it_is_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_token(monkeypatch, None)

    with pytest.raises(PermissionError, match=REFUSAL):
        _request_context("demo", "source_acl", None, _Listing({}))
    listed = _Listing({"local-unauthenticated": {"demo"}})
    assert str(_request_context("demo", "source_acl", None, listed).access.tenant_id) == "demo"


@pytest.mark.asyncio
async def test_mcp_refuses_unlisted_principal_before_the_tool_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_token(monkeypatch, _reader("stranger", "demo"))
    server = _server(tmp_path, {"someone-else": {"demo"}})
    handler = _tool_handler(server, "list_sources")

    with pytest.raises(PermissionError, match=REFUSAL):
        await handler(tenant_id="demo")

    assert _calls(server) == []
    assert server.audit.entries[-1]["outcome"] == "error"
    assert server.audit.entries[-1]["error_type"] == "PermissionError"


@pytest.mark.asyncio
async def test_a_listed_principal_reaches_the_tool_through_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_token(monkeypatch, _reader("reader-1", "demo"))
    server = _server(tmp_path, {"reader-1": {"demo"}})

    await _tool_handler(server, "list_sources")(tenant_id="demo")

    assert _calls(server) == ["list_sources"]


@pytest.mark.asyncio
async def test_a_tenant_free_tool_still_needs_the_principal_to_be_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``describe_graph`` reads no tenant, so a listing in any tenant is enough."""

    _as_token(monkeypatch, _reader("reader-1", "*"))
    stranger = _server(tmp_path, {"someone-else": {"demo"}})
    listed = _server(tmp_path, {"reader-1": {"demo"}})

    with pytest.raises(PermissionError, match=REFUSAL):
        await _tool_handler(stranger, "describe_graph")()
    await _tool_handler(listed, "describe_graph")()

    assert _calls(stranger) == []
    assert _calls(listed) == ["describe_graph"]


@contextmanager
def _real_token(subject: str, *tenants: str) -> Iterator[None]:
    """Authenticate the way the transport does, so ``get_access_token`` is the real one."""

    from fastmcp.server.auth import AccessToken
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    claims = {"sub": subject, "role": "reader", "tenants": list(tenants)}
    token = AccessToken(token="t", client_id="client", scopes=[], claims=claims)
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


@pytest.mark.asyncio
async def test_the_real_token_reaches_the_worker_thread_that_reads_the_file(
    tmp_path: Path,
) -> None:
    """The token lives in a context variable, which the file check's thread must inherit."""

    pytest.importorskip("fastmcp")
    server = _server(tmp_path, {"reader-1": {"demo"}})

    with _real_token("reader-1", "demo"):
        await _tool_handler(server, "list_sources")(tenant_id="demo")

    assert _calls(server) == ["list_sources"]
    listing = server.access_watcher
    assert isinstance(listing, _Listing)
    assert listing.threads
    assert threading.get_ident() not in listing.threads


@pytest.mark.asyncio
async def test_a_real_unlisted_token_is_refused_by_its_own_identity(tmp_path: Path) -> None:
    pytest.importorskip("fastmcp")
    # Listing only the local stdio identity proves the refusal reads the token's subject.
    server = _server(tmp_path, {"local-unauthenticated": {"demo"}})

    with _real_token("stranger", "demo"), pytest.raises(PermissionError, match=REFUSAL):
        await _tool_handler(server, "list_sources")(tenant_id="demo")

    assert _calls(server) == []


def _app(server: McpServer, tmp_path: Path) -> Starlette:
    configuration = McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration(),
        specs=server.list_tools(),
        audit=server.audit,
        environment={},
    )
    server.configuration = configuration
    verifier = create_local_token_verifier(TOKEN)
    transport = create_mcp_server(registry=server, auth=verifier)
    register_http_routes(
        transport,
        mcp_path="/mcp",
        registry=server,
        configuration=configuration,
        token_verifier=verifier,
    )
    return transport.http_app(path="/mcp")


async def _post_call(app: Starlette, body: dict[str, object]) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                "/api/tools/call", headers={"Authorization": f"Bearer {TOKEN}"}, json=body
            )


@pytest.mark.asyncio
async def test_http_tool_call_refuses_an_unlisted_principal_before_the_tool_runs(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path, {"someone-else": {"demo"}})

    response = await _post_call(
        _app(server, tmp_path),
        {"name": "vector_search", "arguments": {"query": "harbor", "tenant_id": "demo"}},
    )

    assert response.status_code == 403
    assert response.json()["error"] == REFUSAL
    assert _calls(server) == []
    # The refusal happens inside the audited call, so the trail shows it.
    assert server.audit.entries[-1]["error_type"] == "PermissionError"


@pytest.mark.asyncio
async def test_http_tool_call_serves_a_listed_principal_in_its_listed_tenant_only(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path, {LOCAL_OWNER: {"demo"}})
    app = _app(server, tmp_path)

    listed = await _post_call(
        app, {"name": "vector_search", "arguments": {"query": "harbor", "tenant_id": "demo"}}
    )
    elsewhere = await _post_call(
        app, {"name": "vector_search", "arguments": {"query": "harbor", "tenant_id": "other"}}
    )

    assert (listed.status_code, elsewhere.status_code) == (200, 403)
    assert _calls(server) == ["vector_search"]


@pytest.mark.asyncio
async def test_http_tool_free_call_needs_a_listed_principal(tmp_path: Path) -> None:
    stranger = _server(tmp_path, {"someone-else": {"demo"}})
    listed = _server(tmp_path, {LOCAL_OWNER: {"demo"}})

    refused = await _post_call(
        _app(stranger, tmp_path), {"name": "describe_graph", "arguments": {}}
    )
    served = await _post_call(_app(listed, tmp_path), {"name": "describe_graph", "arguments": {}})

    assert (refused.status_code, served.status_code) == (403, 200)
    assert _calls(stranger) == []


@pytest.mark.asyncio
async def test_without_a_watcher_nothing_is_checked(tmp_path: Path) -> None:
    server = _server(tmp_path, None)

    response = await _post_call(
        _app(server, tmp_path),
        {"name": "vector_search", "arguments": {"query": "harbor", "tenant_id": "demo"}},
    )

    assert response.status_code == 200
    assert _calls(server) == ["vector_search"]
