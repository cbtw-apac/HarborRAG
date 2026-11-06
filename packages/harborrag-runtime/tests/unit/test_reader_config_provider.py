"""ReaderApplication owns a settings provider only over Postgres."""

from __future__ import annotations

import pytest

from harborrag_runtime.composition.readers import ReaderApplication
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_reader_application_builds_provider_for_postgres_only(tmp_path) -> None:
    settings = RuntimeSettings(control_db_url=f"sqlite+aiosqlite:///{tmp_path}/control.db")
    application = ReaderApplication(settings)

    await application._start_config_provider()  # noqa: SLF001 - start() also opens retrieval backends

    assert application.config_provider is None
    await application.aclose()
