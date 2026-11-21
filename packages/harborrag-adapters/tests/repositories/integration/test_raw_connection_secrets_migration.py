"""Migration 0049 clears raw secret values that connection rows still hold."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command

import harborrag_adapters.repositories.database.control_plane as control_plane
from harborrag_adapters.repositories.database.control_plane.connections import (
    SqlConnectionRepository,
)
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.migrations import (
    _build_config,
    run_migrations,
)
from harborrag_core.domain.connection import Connection
from harborrag_core.domain.validation import validate_secret_free_config

pytestmark = [pytest.mark.integration, pytest.mark.whitebox]

_MIGRATION = (
    Path(control_plane.__file__).parent
    / "alembic"
    / "versions"
    / "0049_clear_raw_connection_secrets.py"
)
_NOW = "2026-01-01 00:00:00+00:00"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0049", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _execute(database: Path, statement: str, rows: list[dict[str, Any]]) -> None:
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            for row in rows:
                connection.execute(sa.text(statement), row)
    finally:
        engine.dispose()


def _rows(database: Path) -> dict[str, dict[str, Any]]:
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            result = connection.execute(sa.text("SELECT * FROM sources ORDER BY id")).mappings()
            return {str(row["id"]): dict(row) for row in result}
    finally:
        engine.dispose()


def _json(row: dict[str, Any], column: str) -> Any:
    value = row[column]
    return json.loads(value) if isinstance(value, str) else value


def _project(database: Path) -> None:
    _execute(
        database,
        "INSERT INTO projects (id, tenant_id, name, description, collection, status,"
        " created_at, updated_at, documents, chunks, size_bytes)"
        " VALUES ('demo', 'acme', 'Demo', '', 'demo', 'active', :now, :now, 0, 0, 0)",
        [{"now": _NOW}],
    )


async def _listed(dsn: str) -> list[Connection]:
    engine = create_control_plane_engine(dsn)
    try:
        connections, _ = await SqlConnectionRepository(create_session_factory(engine)).list(
            tenant_ids=None
        )
        return connections
    finally:
        await engine.dispose()


def test_0049_clears_a_legacy_raw_token_and_lists_the_connection(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    command.upgrade(_build_config(dsn), "0040")
    _project(database)
    _execute(
        database,
        "INSERT INTO sources (id, tenant_id, project_id, source_type, name, config_json,"
        " secret_refs, status, documents, chunks, errors)"
        " VALUES ('s1', 'acme', 'demo', 'jira', 'Demo tickets', :config, '[]', 'active',"
        " 0, 0, 0)",
        [
            {
                "config": json.dumps(
                    {
                        "base_url": "https://jira.example.com",
                        "email": "alex@example.com",
                        "token": "demo-token-000",
                    }
                )
            }
        ],
    )

    run_migrations(dsn)
    row = _rows(database)["s1"]
    listed = asyncio.run(_listed(dsn))

    assert "demo-token-000" not in json.dumps(row, default=str)
    assert _json(row, "credentials") == {
        "method": "api_token",
        "base_url": "https://jira.example.com",
        "email": "alex@example.com",
    }
    assert row["status"] == "error"
    assert [error["field"] for error in _json(row, "status_detail")["errors"]] == [
        "credentials.api_token"
    ]
    assert row["version"] == 2 and row["updated_by"] == "system"
    assert [connection.connection_id for connection in listed] == ["demo-tickets"]


_SOURCES_0048 = (
    "INSERT INTO sources (id, tenant_id, project_id, connection_id, source_key, source_type,"
    " name, credentials, config_json, tuning, secret_refs, status, status_detail, version,"
    " created_by, updated_by, created_at, updated_at, documents, chunks, errors)"
    " VALUES (:id, 'acme', 'demo', :id, :key, 'jira', :id, :credentials, :config, :tuning,"
    " '[]', :status, :detail, 1, 'system', 'system', :now, :now, 0, 0, 0)"
)


def _source(
    source_id: str,
    *,
    credentials: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    tuning: dict[str, Any] | None = None,
    status: str = "active",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": source_id,
        "key": f"demo/{source_id}",
        "credentials": json.dumps(credentials or {}),
        "config": json.dumps(config or {}),
        "tuning": json.dumps(tuning or {}),
        "status": status,
        "detail": json.dumps(detail) if detail is not None else None,
        "now": _NOW,
    }


def test_0049_clears_every_kind_of_raw_value_and_keeps_references(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    config = _build_config(dsn)
    command.upgrade(config, "0048")
    _project(database)
    unknown_type = {"errors": [{"field": "source_type", "issue": "unknown source type"}]}
    _execute(
        database,
        _SOURCES_0048,
        [
            _source(
                "nested",
                credentials={"method": "pat", "token": "env:DEMO_TOKEN"},
                config={
                    "auth": {"password": "demo-password-000"},
                    "rules": [{"api_key": "demo-key-000"}],
                },
                tuning={"clientSecret": "demo-secret-000", "page_size": 50},
            ),
            _source(
                "deleting",
                credentials={"token": "demo-token-001"},
                status="deleting",
                detail={"job_id": "job-1", "purge": True},
            ),
            _source("empty", credentials={"method": "none", "token": ""}),
            _source(
                "errored",
                config={"token": "demo-token-002"},
                status="error",
                detail=unknown_type,
            ),
            _source(
                "clean",
                credentials={
                    "api_token": {"secret_ref": "secret://db/a", "last4": None},
                    "bearer": "secret://db/b",
                    "token_cache_path": "/var/cache/harborrag",
                },
                config={"token_budget": 5, "jql": "project = DEMO"},
            ),
        ],
    )

    command.upgrade(config, "head")
    rows = _rows(database)

    assert "demo-" not in json.dumps(rows, default=str)
    for row in rows.values():
        for column in ("credentials", "config_json", "tuning"):
            validate_secret_free_config(_json(row, column))
    nested = rows["nested"]
    assert nested["status"] == "error"
    assert [error["field"] for error in _json(nested, "status_detail")["errors"]] == [
        "config.auth.password",
        "config.rules[0].api_key",
        "credentials.token",
        "tuning.clientSecret",
    ]
    assert _json(nested, "tuning") == {"page_size": 50}
    assert (rows["deleting"]["status"], _json(rows["deleting"], "status_detail")) == (
        "deleting",
        {"job_id": "job-1", "purge": True},
    )
    assert (rows["empty"]["status"], rows["empty"]["status_detail"]) == ("active", None)
    assert _json(rows["empty"], "credentials") == {"method": "none"}
    assert [error["field"] for error in _json(rows["errored"], "status_detail")["errors"]] == [
        "source_type",
        "config.token",
    ]
    assert rows["clean"]["version"] == 1
    assert rows["clean"]["status"] == "active"

    command.downgrade(config, "0048")
    command.upgrade(config, "head")
    assert _rows(database) == rows


_PARITY_SAMPLES: tuple[dict[str, Any], ...] = (
    {"token": "demo-token-000"},
    {"token": "secret://db/a"},
    {"token": "env:DEMO_TOKEN"},
    {"token": {"secret_ref": "env:DEMO_TOKEN"}},
    {"token": {"secret_ref": "secret://db/a", "last4": "0000", "updated_at": None}},
    {"token": {"secret_ref": "secret://db/a", "extra": 1}},
    {"token": None},
    {"token_budget": 5, "max_tokens": 10, "token_count": 2},
    {"token_cache_path": "/var/cache/harborrag", "token_file": "/run/token"},
    {"apiKey": "x", "x-apikey": "y", "clientSecret": "z", "passWord": "w"},
    {"bearertoken": "x", "authorization": "Basic x", "ssh_private_key": "k"},
    {"allow_ambient_credentials": True, "secrets": ["secret://db/a"], "secretary": "x"},
    {"auth": {"password": "demo"}, "rules": [{"api_key": "demo"}, "token"]},
    {"refs": [{"secret_ref": "env:DEMO_TOKEN"}], "tokenizer": "cl100k"},
)


@pytest.mark.parametrize("sample", _PARITY_SAMPLES)
def test_0049_rules_match_the_secret_free_validator(sample: dict[str, Any]) -> None:
    migration = _migration()
    removed: dict[str, bool] = {}

    cleaned = migration._strip(sample, "config", removed)

    validate_secret_free_config(cleaned)
    try:
        validate_secret_free_config(sample)
    except ValueError:
        assert removed
    else:
        assert not removed and cleaned == sample
