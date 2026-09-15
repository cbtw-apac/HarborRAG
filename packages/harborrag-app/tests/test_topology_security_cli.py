"""Trusted operator inputs are explicit, bounded and never trigger model calls."""

import json
from datetime import timedelta

from typer.testing import CliRunner

from harborrag_app.cli.main import app
from harborrag_core.base import utc_now
from harborrag_runtime.topology import operations, security_operations


def test_indexing_configure_keeps_disabled_default_and_separate_pause(tmp_path, monkeypatch):
    received = []

    async def configure(_settings, config):
        received.append(config)
        return {"enabled": config.enabled, "spending_paused": config.spending_paused}

    monkeypatch.setattr(security_operations, "configure_indexing", configure)
    path = tmp_path / "tenant.json"
    path.write_text('{"tenant_id":"tenant","spending_paused":true}', encoding="utf-8")
    result = CliRunner().invoke(app, ["topology", "indexing", "configure", str(path)])
    assert result.exit_code == 0, result.output
    assert not received[0].enabled and received[0].spending_paused


def test_permission_import_requires_resolved_schema_and_does_not_echo_private_errors(
    tmp_path, monkeypatch
):
    received = []

    async def import_snapshot(_settings, snapshot):
        received.append(snapshot)
        return {"revision": snapshot.revision}

    monkeypatch.setattr(security_operations, "import_permissions", import_snapshot)
    now = utc_now()
    path = tmp_path / "permission.json"
    path.write_text(
        json.dumps(
            {
                "tenant_id": "tenant",
                "resource_kind": "source",
                "resource_id": "scope",
                "revision": "r1",
                "resolved_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "known": True,
                "processing_allowed": True,
                "allowed_principal_ids": ["reader"],
            }
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["topology", "indexing", "permissions-import", str(path)])
    assert result.exit_code == 0, result.output
    assert received[0].allowed_principal_ids == ("reader",)
    path.write_text('{"raw_group":"private-secret-group"}', encoding="utf-8")
    result = CliRunner().invoke(app, ["topology", "indexing", "permissions-import", str(path)])
    assert result.exit_code != 0 and "private-secret-group" not in result.output


def test_entity_inspection_requires_explicit_principal(monkeypatch):
    received = []

    async def entities(_settings, tenant, label, *, principal_id):
        received.append((tenant, label, principal_id))
        return []

    monkeypatch.setattr(operations, "entities", entities)
    assert CliRunner().invoke(app, ["topology", "entities"]).exit_code != 0
    result = CliRunner().invoke(
        app,
        [
            "topology",
            "entities",
            "--tenant",
            "tenant",
            "--principal",
            "reader",
            "--label",
            "service",
        ],
    )
    assert result.exit_code == 0, result.output
    assert received == [("tenant", "service", "reader")]


def test_build_inspection_requires_principal_and_forwards_bounds(monkeypatch):
    received = []

    async def inspect_build(_settings, tenant, build_id, *, principal_id, limit):
        received.append((tenant, build_id, principal_id, limit))
        return {"build": {"build_id": build_id}}

    monkeypatch.setattr(operations, "inspect_build", inspect_build)
    assert CliRunner().invoke(app, ["topology", "inspect", "build-1"]).exit_code != 0
    result = CliRunner().invoke(
        app,
        [
            "topology",
            "inspect",
            "build-1",
            "--tenant",
            "tenant",
            "--principal",
            "reader",
            "--limit",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert received == [("tenant", "build-1", "reader", 3)]
