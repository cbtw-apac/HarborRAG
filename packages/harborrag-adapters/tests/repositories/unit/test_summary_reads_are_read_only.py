"""The reader-facing summary operations never write, so a read-only role can serve them."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event

from harborrag_core.schemas.ids import TenantId
from harborrag_core.security.context import AccessContext

from .ingestion_control_fixtures import make_control_plane

_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "MERGE")


@pytest.mark.asyncio
async def test_entity_evidence_and_views_issue_only_selects(tmp_path: Path) -> None:
    # No rows exist for this tenant, which is exactly when the old code inserted
    # the summary_tenants and topology_indexing_configs rows it wanted to lock.
    access = AccessContext(principal_id="reader", tenant_id=TenantId("DEFAULT"))
    async with make_control_plane(tmp_path) as control:
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):  # noqa: PLR0913
            statements.append(statement.strip().upper())

        engine = control.summaries._client.raw.sync_engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            assert (
                await control.summaries.entity_evidence("DEFAULT", ("node-1",), access=access) == {}
            )
            views = await control.summaries.views(
                "DEFAULT",
                ("node-1",),
                access=access,
                source_scopes={"node-1": "@tenant"},
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)

    assert "node-1" not in views or views["node-1"].card is None
    assert statements, "the reads must have hit the database"
    writes = [s for s in statements if s.startswith(_WRITE_VERBS) or " FOR UPDATE" in s]
    assert writes == []
