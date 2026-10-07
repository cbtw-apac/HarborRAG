"""Migration 0038 admits PURGED document versions and round-trips."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.database.control_plane.migrations import _build_config

_DOCUMENT = (
    "INSERT INTO documents (document_id,tenant_id,source_scope_id,connector_type,connection_id,"
    "source_item_id,created_at,updated_at) VALUES ('doc','tenant','scope','local','conn','item',"
    "'2026-10-01','2026-10-01')"
)


def _version(version_id: str, status: str) -> str:
    return (
        "INSERT INTO document_versions (document_version_id,source_scope_id,document_id,"
        "canonical_content_hash,retrieval_metadata_hash,processing_fingerprint,"
        "admission_change_key,status,created_at,updated_at) VALUES "
        f"('{version_id}','scope','doc','c','r','p','a','{status}','2026-10-01','2026-10-01')"
    )


def test_purged_status_migration_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "purged.db"
    config = _build_config(f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "0037")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            connection.execute(text(_DOCUMENT))
            connection.execute(text(_version("v-retired", "RETIRED")))
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(_version("v-early", "PURGED")))

        command.upgrade(config, "0038")
        with engine.begin() as connection:
            connection.execute(text(_version("v-purged", "PURGED")))
            connection.execute(text(_version("v-active", "ACTIVE")))
        # The batch rebuild keeps the one-active-version partial index.
        indexes = {index["name"] for index in inspect(engine).get_indexes("document_versions")}
        assert "uq_document_one_active_version" in indexes
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(_version("v-active-2", "ACTIVE")))
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(_version("v-bogus", "BOGUS")))

        command.downgrade(config, "0037")
        with engine.connect() as connection:
            statuses = dict(
                connection.execute(
                    text("SELECT document_version_id, status FROM document_versions")
                ).all()
            )
        assert statuses == {
            "v-retired": "RETIRED",
            "v-purged": "RETIRED",
            "v-active": "ACTIVE",
        }
    finally:
        engine.dispose()
