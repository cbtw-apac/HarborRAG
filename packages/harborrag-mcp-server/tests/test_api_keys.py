from __future__ import annotations

import hashlib
import logging
import os
import threading
import time

import pytest

from harborrag_core.contracts.tools import BaseTool, ToolInvocationContext, ToolSpec
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.server import api_keys
from harborrag_mcp_server.server.api_keys import create_api_key_verifier
from harborrag_mcp_server.server.server import McpServer


@pytest.mark.asyncio
async def test_reader_key_is_tenant_bound_and_revocable(tmp_path, monkeypatch):
    secret = "reader-key-" + "a" * 40
    monkeypatch.setenv("TEST_MCP_KEY_HASH", hashlib.sha256(secret.encode()).hexdigest())
    path = tmp_path / "keys.yaml"
    path.write_text(
        "version: 1\nkeys:\n"
        "  - key_id: engineering\n"
        "    principal_id: mcp-reader:engineering\n"
        "    tenant_id: DEFAULT\n"
        "    secret_hash_env: TEST_MCP_KEY_HASH\n",
        encoding="utf-8",
    )
    verifier = create_api_key_verifier(path)
    assert await verifier.verify_token("wrong" * 10) is None
    token = await verifier.verify_token(secret)
    assert token is not None
    assert token.claims["tenants"] == ["DEFAULT"]
    assert token.claims["role"] == "reader"
    assert token.claims["sub"] == "mcp-reader:engineering"
    path.write_text(path.read_text(encoding="utf-8") + "    revoked: true\n", encoding="utf-8")
    assert await verifier.verify_token(secret) is None


class ModeProbe(BaseTool):
    spec = ToolSpec(
        name="mode_probe",
        description="Inspect the server-bound policy in a test.",
        input_schema={
            "type": "object",
            "properties": {"tenant_id": {"type": "string"}},
            "required": ["tenant_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"mode": {"type": "string"}}},
    )

    async def call(self, arguments, *, principal_id, context: ToolInvocationContext | None = None):
        assert context is not None
        return {"mode": context.access.corpus_mode}


@pytest.mark.asyncio
async def test_shared_mode_is_only_applied_to_its_configured_tenant() -> None:
    server = McpServer(
        tools=[ModeProbe()],
        audit=McpAuditLog(),
        corpus_mode="tenant_shared",
        shared_tenant_id="DEFAULT",
    )
    assert (await server.call_tool("mode_probe", {"tenant_id": "DEFAULT"}))["mode"] == (
        "tenant_shared"
    )
    assert (await server.call_tool("mode_probe", {"tenant_id": "RESTRICTED"}))["mode"] == (
        "source_acl"
    )


@pytest.mark.parametrize("tenant", ["'*'", "' * '", "' DEFAULT'"])
def test_reader_key_rejects_wildcard_or_padded_tenant(tmp_path, monkeypatch, tenant):
    monkeypatch.setenv("TEST_MCP_KEY_HASH", "a" * 64)
    path = tmp_path / "keys.yaml"
    path.write_text(
        "version: 1\nkeys:\n"
        "  - key_id: engineering\n"
        "    principal_id: mcp-reader:engineering\n"
        f"    tenant_id: {tenant}\n"
        "    secret_hash_env: TEST_MCP_KEY_HASH\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="tenant_id"):
        create_api_key_verifier(path)


def _write_key_file(path, *entries: str, age: float | None = None) -> None:
    path.write_text("version: 1\nkeys:\n" + "".join(entries), encoding="utf-8")
    if age is not None:
        # Old enough that the verifier trusts its cached parse of this version.
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))


def _key_entry(key_id: str, env: str, extra: str = "") -> str:
    return (
        f"  - key_id: {key_id}\n"
        f"    principal_id: mcp-reader:{key_id}\n"
        "    tenant_id: DEFAULT\n"
        f"    secret_hash_env: {env}\n" + extra
    )


@pytest.fixture
def reader_secret(monkeypatch) -> str:
    secret = "reader-key-" + "b" * 40
    monkeypatch.setenv("TEST_MCP_KEY_HASH", hashlib.sha256(secret.encode()).hexdigest())
    monkeypatch.delenv("TEST_MCP_UNSET_HASH", raising=False)
    return secret


@pytest.mark.asyncio
async def test_appending_key_with_unset_hash_env_keeps_other_keys(
    tmp_path, reader_secret, caplog
) -> None:
    path = tmp_path / "keys.yaml"
    _write_key_file(path, _key_entry("engineering", "TEST_MCP_KEY_HASH"))
    verifier = create_api_key_verifier(path)
    assert await verifier.verify_token(reader_secret) is not None

    # The new key's hash variable cannot reach a process that is already running.
    _write_key_file(
        path,
        _key_entry("engineering", "TEST_MCP_KEY_HASH"),
        _key_entry("support", "TEST_MCP_UNSET_HASH"),
        "  - key_id: malformed\n",
    )
    with caplog.at_level(logging.WARNING, logger="harborrag.mcp.server.api_keys"):
        assert await verifier.verify_token(reader_secret) is not None

    assert "Skipping MCP key support" in caplog.text
    assert "Skipping MCP key malformed" in caplog.text


@pytest.mark.asyncio
async def test_unparseable_reload_denies_every_key_until_the_file_parses(
    tmp_path, reader_secret, caplog, monkeypatch
) -> None:
    path = tmp_path / "keys.yaml"
    _write_key_file(path, _key_entry("engineering", "TEST_MCP_KEY_HASH"))
    verifier = create_api_key_verifier(path)

    # An operator revokes a leaked key but saves invalid YAML: keeping the old
    # key set would leave the leaked key live, so a broken file fails closed.
    path.write_text("version: 1\nkeys: [\n  - key_id: engineering\n    revoked: true\n")
    stamp = time.time() - 60
    os.utime(path, (stamp, stamp))
    loads: list[int] = []
    original = api_keys._load_keys

    def recording_load(*args, **kwargs):
        loads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(api_keys, "_load_keys", recording_load)
    with caplog.at_level(logging.ERROR, logger="harborrag.mcp.server.api_keys"):
        assert await verifier.verify_token(reader_secret) is None
        assert await verifier.verify_token(reader_secret) is None

    assert len(loads) == 1, "the failed version is cached, not re-parsed per request"
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "denying every key" in errors[0].getMessage()

    _write_key_file(path, _key_entry("engineering", "TEST_MCP_KEY_HASH"))
    assert await verifier.verify_token(reader_secret) is not None


@pytest.mark.asyncio
async def test_reload_skips_duplicate_key_ids_and_honours_revocation(
    tmp_path, reader_secret
) -> None:
    path = tmp_path / "keys.yaml"
    _write_key_file(path, _key_entry("engineering", "TEST_MCP_KEY_HASH"))
    verifier = create_api_key_verifier(path)

    # A live duplicate must not shadow the revoked copy of the same key.
    _write_key_file(
        path,
        _key_entry("engineering", "TEST_MCP_KEY_HASH", "    revoked: true\n"),
        _key_entry("engineering", "TEST_MCP_KEY_HASH"),
    )
    assert await verifier.verify_token(reader_secret) is None


@pytest.mark.asyncio
async def test_missing_key_file_denies_every_key(tmp_path, reader_secret) -> None:
    path = tmp_path / "keys.yaml"
    _write_key_file(path, _key_entry("engineering", "TEST_MCP_KEY_HASH"))
    verifier = create_api_key_verifier(path)

    path.unlink()

    assert await verifier.verify_token(reader_secret) is None


@pytest.mark.asyncio
async def test_emptied_key_list_revokes_every_key(tmp_path, reader_secret) -> None:
    path = tmp_path / "keys.yaml"
    _write_key_file(path, _key_entry("engineering", "TEST_MCP_KEY_HASH"))
    verifier = create_api_key_verifier(path)

    path.write_text("version: 1\nkeys: []\n", encoding="utf-8")

    assert await verifier.verify_token(reader_secret) is None


@pytest.mark.asyncio
async def test_unchanged_key_file_is_cached_and_reloaded_off_the_event_loop(
    tmp_path, reader_secret, monkeypatch
) -> None:
    path = tmp_path / "keys.yaml"
    _write_key_file(path, _key_entry("engineering", "TEST_MCP_KEY_HASH"), age=60)
    verifier = create_api_key_verifier(path)
    loads: list[int] = []
    original = api_keys._load_keys

    def recording_load(*args, **kwargs):
        loads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(api_keys, "_load_keys", recording_load)
    for _ in range(3):
        assert await verifier.verify_token(reader_secret) is not None
    assert loads == []

    _write_key_file(
        path, _key_entry("engineering", "TEST_MCP_KEY_HASH", "    revoked: true\n"), age=30
    )
    assert await verifier.verify_token(reader_secret) is None
    assert len(loads) == 1
    assert loads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_expired_key_denial_is_logged_by_key_id(tmp_path, reader_secret, caplog) -> None:
    path = tmp_path / "keys.yaml"
    _write_key_file(
        path,
        _key_entry("engineering", "TEST_MCP_KEY_HASH", "    expires_at: 2020-01-01T00:00:00Z\n"),
    )
    verifier = create_api_key_verifier(path)

    with caplog.at_level(logging.WARNING, logger="harborrag.mcp.server.api_keys"):
        assert await verifier.verify_token(reader_secret) is None
        assert await verifier.verify_token(reader_secret) is None

    denials = [record for record in caplog.records if "revoked or expired" in record.getMessage()]
    assert len(denials) == 1
    assert "engineering" in denials[0].getMessage()
    assert reader_secret not in caplog.text
