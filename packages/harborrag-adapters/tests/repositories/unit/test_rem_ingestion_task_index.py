from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from harborrag_adapters.repositories.database.ingestion_control.schema import INGESTION_TASKS

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_the_task_listing_index_is_ascending_as_revision_0019_creates_it() -> None:
    index = next(
        index
        for index in INGESTION_TASKS.indexes
        if index.name == "ix_ingestion_tasks_tenant_submitted"
    )

    statement = str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    assert statement.endswith("(tenant_id, submitted_at, task_id)")
