from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from harborrag_adapters.repositories.database.control_plane.migrations import (
    _build_config,
    run_migrations,
)

pytestmark = [pytest.mark.integration, pytest.mark.whitebox]

_NOW = "2026-01-01 00:00:00+00:00"


def _seed_raw_token(database: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO projects (id, tenant_id, name, description, collection, status,"
                    " created_at, updated_at, documents, chunks, size_bytes)"
                    " VALUES ('demo', 'acme', 'Demo', '', 'demo', 'active', :now, :now, 0, 0, 0)"
                ),
                {"now": _NOW},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO sources (id, tenant_id, project_id, source_type, name,"
                    " config_json, secret_refs, status, documents, chunks, errors)"
                    " VALUES ('s1', 'acme', 'demo', 'jira', 'Demo tickets', :config, '[]',"
                    " 'active', 0, 0, 0)"
                ),
                {
                    "config": json.dumps(
                        {
                            "base_url": "https://jira.example.com",
                            "email": "alex@example.com",
                            "token": "demo-token-000",
                        }
                    )
                },
            )
    finally:
        engine.dispose()


def test_0049_logs_do_not_claim_secrets_were_cleared_before_commit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "control.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    command.upgrade(_build_config(dsn), "0040")
    _seed_raw_token(database)

    with caplog.at_level(logging.WARNING, logger="harborrag.adapters.control_plane.migrations"):
        run_migrations(dsn)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "harborrag.adapters.control_plane.migrations"
    ]
    assert messages == [
        "Clearing raw connection secrets connection_pk=s1 tenant_id=acme fields=credentials.api_token",
        "Clearing raw connection secrets connections=1; the change takes effect when the "
        "migration commits",
    ]
    assert all("demo-token-000" not in message for message in messages)
