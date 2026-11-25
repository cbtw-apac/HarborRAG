from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from harborrag_adapters.repositories.database.control_plane.schemas import Base

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

_JSON_IN_DATABASE = (
    ("sources", "config_json"),
    ("sources", "secret_refs"),
    ("jobs", "payload_json"),
    ("job_events", "payload_json"),
    ("pending_control_plane_effects", "payload_json"),
    ("providers", "config_json"),
    ("routing_rules", "rule_json"),
    ("workspace_settings", "data"),
    ("memories", "metadata_json"),
)
_JSONB_IN_DATABASE = (
    ("sources", "credentials"),
    ("sources", "tuning"),
    ("sources", "status_detail"),
    ("connection_revisions", "snapshot_json"),
    ("connection_revisions", "secret_refs"),
    ("setting_overrides", "value_json"),
    ("setting_revisions", "changes_json"),
)


def _postgres_type(table: str, column: str) -> str:
    column_type = Base.metadata.tables[table].c[column].type
    return str(column_type.compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize(("table", "column"), _JSON_IN_DATABASE)
def test_a_column_its_revision_created_as_json_is_json_on_postgres(table: str, column: str) -> None:
    assert _postgres_type(table, column) == "JSON"


@pytest.mark.parametrize(("table", "column"), _JSONB_IN_DATABASE)
def test_a_column_a_later_revision_created_as_jsonb_stays_jsonb(table: str, column: str) -> None:
    assert _postgres_type(table, column) == "JSONB"
