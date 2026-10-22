"""Alpha2 migrations keep legacy rows usable: secret tenants up, PAUSED tasks down."""

from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, text

from harborrag_adapters.repositories.database.control_plane.migrations import _build_config

_NOW = "2026-10-01 00:00:00"


def _secret(ref: str) -> str:
    return (
        "INSERT INTO secrets (ref,provider,created_by,created_at,ciphertext) "
        f"VALUES ('{ref}','db','admin','{_NOW}',X'00')"
    )


def _source(source_id: str, tenant_id: str, refs: str) -> str:
    return (
        "INSERT INTO sources (id,tenant_id,project_id,source_type,name,config_json,secret_refs,"
        f"status,documents,chunks,errors) VALUES ('{source_id}','{tenant_id}','p-{tenant_id}',"
        f"'confluence','wiki','{{}}','{refs}','active',0,0,0)"
    )


def _project(tenant_id: str) -> str:
    return (
        "INSERT INTO projects (id,tenant_id,name,description,collection,status,created_at,"
        f"updated_at,documents,chunks,size_bytes) VALUES ('p-{tenant_id}','{tenant_id}','p','',"
        f"'c','active','{_NOW}','{_NOW}',0,0,0)"
    )


def test_secret_tenants_are_backfilled_from_the_sources_that_own_them(tmp_path: Path) -> None:
    """R2-06: an alpha1 source's secrets keep their source's tenant, not DEFAULT."""

    path = tmp_path / "secrets.db"
    config = _build_config(f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "0031")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            for tenant_id in ("acme", "globex", "DEFAULT"):
                connection.execute(text(_project(tenant_id)))
            for ref in ("secret://db/acme", "secret://db/shared", "secret://db/orphan"):
                connection.execute(text(_secret(ref)))
            connection.execute(text(_secret("secret://db/default")))
            connection.execute(
                text(_source("s-acme", "acme", '["secret://db/acme","secret://db/shared"]'))
            )
            connection.execute(text(_source("s-globex", "globex", '["secret://db/shared"]')))
            connection.execute(text(_source("s-default", "DEFAULT", '["secret://db/default"]')))

        command.upgrade(config, "0032")

        with engine.connect() as connection:
            tenants = dict(connection.execute(text("SELECT ref, tenant_id FROM secrets")).all())
        assert tenants == {
            "secret://db/acme": "acme",
            # Referenced by two tenants: ambiguous, so it keeps the fallback.
            "secret://db/shared": "DEFAULT",
            # Referenced by nothing.
            "secret://db/orphan": "DEFAULT",
            "secret://db/default": "DEFAULT",
        }
    finally:
        engine.dispose()


def test_paused_tasks_downgrade_to_running(tmp_path: Path) -> None:
    """R2-05: rolling back past 0035 must not abort on a paused ingestion task."""

    path = tmp_path / "paused.db"
    config = _build_config(f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "0035")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            for task_id, status in (("t-paused", "PAUSED"), ("t-done", "COMPLETED")):
                connection.execute(
                    text(
                        "INSERT INTO ingestion_tasks (task_id,tenant_id,source_scope_id,status,"
                        f"submitted_at,request,summary) VALUES ('{task_id}','DEFAULT','s',"
                        f"'{status}','{_NOW}','{{}}','{{}}')"
                    )
                )

        command.downgrade(config, "0034")

        with engine.connect() as connection:
            statuses = dict(
                connection.execute(text("SELECT task_id, status FROM ingestion_tasks")).all()
            )
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert statuses == {"t-paused": "RUNNING", "t-done": "COMPLETED"}
        assert revision == "0034"
    finally:
        engine.dispose()
