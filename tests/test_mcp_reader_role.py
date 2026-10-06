"""The MCP database role grants exactly what the reader tools need."""

from __future__ import annotations

import re
from pathlib import Path

from harborrag_adapters.repositories.database.control_plane.schemas import Base as CONTROL_PLANE
from harborrag_adapters.repositories.database.ingestion_control.schema import METADATA as INGESTION

ROOT = Path(__file__).resolve().parents[1]
ROLE_SQL = ROOT / "deploy/postgres/mcp-reader-role.sql"

_GRANT = re.compile(r'GRANT (SELECT|INSERT|UPDATE) ON TABLE\s+(.*?)\s+TO :"mcp_user";', re.DOTALL)


def _grants() -> dict[str, set[str]]:
    sql = ROLE_SQL.read_text(encoding="utf-8")
    grants: dict[str, set[str]] = {}
    for privilege, tables in _GRANT.findall(sql):
        grants.setdefault(privilege, set()).update(
            name.strip() for name in tables.split(",") if name.strip()
        )
    return grants


# The hashed reader keys the server verifies bearer tokens against: the only
# control-plane table the role may see.
READABLE_CONTROL_PLANE = {"mcp_api_keys"}


def test_select_covers_every_ingestion_table_and_nothing_else() -> None:
    # An ingestion table missing here fails a reader tool with "permission
    # denied" at runtime; an extra one widens the role past what it serves.
    assert _grants()["SELECT"] == set(INGESTION.tables) | READABLE_CONTROL_PLANE


def test_the_role_is_read_only() -> None:
    sql = ROLE_SQL.read_text(encoding="utf-8")

    assert set(_grants()) == {"SELECT"}
    assert 'ALTER ROLE :"mcp_user" SET default_transaction_read_only = on;' in sql


def test_control_plane_tables_and_broad_privileges_are_never_granted() -> None:
    sql = ROLE_SQL.read_text(encoding="utf-8")
    granted = set().union(*_grants().values())

    assert granted & set(CONTROL_PLANE.metadata.tables) == READABLE_CONTROL_PLANE
    assert "secrets" in CONTROL_PLANE.metadata.tables  # the table this role must never see
    assert not re.search(r"GRANT\s+(ALL|DELETE|TRUNCATE|CREATE)\b", sql)
    assert "ALTER DEFAULT PRIVILEGES" not in sql
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE" in sql


def test_object_store_policy_only_reads_the_artifact_bucket() -> None:
    import json

    from harborrag_adapters.repositories.object_store.ingestion_artifacts import ARTIFACT_BUCKET

    policy = json.loads((ROOT / "deploy/minio/mcp-reader-policy.json").read_text(encoding="utf-8"))
    actions: set[str] = set()
    resources: set[str] = set()
    for statement in policy["Statement"]:
        assert statement["Effect"] == "Allow"
        actions.update(statement["Action"])
        resources.update(statement["Resource"])

    # head_bucket needs ListBucket + GetBucketLocation; the reads are GetObject.
    assert actions == {"s3:GetBucketLocation", "s3:ListBucket", "s3:GetObject"}
    assert resources == {
        f"arn:aws:s3:::{ARTIFACT_BUCKET}",
        f"arn:aws:s3:::{ARTIFACT_BUCKET}/*",
    }


def test_grants_are_applied_atomically_after_a_schema_precondition() -> None:
    sql = ROLE_SQL.read_text(encoding="utf-8")

    precondition = sql.index("to_regclass('public.mcp_api_keys')")
    begin = sql.index("\nBEGIN;\n")
    revoke = sql.index("REVOKE ALL PRIVILEGES ON ALL TABLES")
    commit = sql.index("\nCOMMIT;\n")
    assert precondition < begin < revoke < commit
    assert sql.count("\nBEGIN;\n") == 1 and sql.count("\nCOMMIT;\n") == 1
