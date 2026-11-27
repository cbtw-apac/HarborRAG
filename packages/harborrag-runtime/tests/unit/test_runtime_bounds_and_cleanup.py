"""Bounds on retention and connector retries, store cleanup on a failed connect, key whitespace."""

from __future__ import annotations

import logging
import os

import pytest
from pydantic import ValidationError

from harborrag_runtime.composition import connection_removal
from harborrag_runtime.composition.connection_removal import ConnectionRemoval
from harborrag_runtime.composition.control_plane import secrets_encryption_key
from harborrag_runtime.config.connectors.models.common import HttpTuning
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.unit

_DSN = "postgresql+asyncpg://demo@database.invalid/control"


@pytest.fixture(autouse=True)
def _no_ambient_harborrag_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [key for key in os.environ if key.startswith("HARBORRAG_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "field",
    [
        "memory_retention_days_session",
        "memory_retention_days_user",
        "memory_retention_days_project",
    ],
)
def test_memory_retention_days_above_a_century_is_refused(field: str) -> None:
    assert getattr(RuntimeSettings(**{field: 36_500}), field) == 36_500
    with pytest.raises(ValidationError, match=field):
        RuntimeSettings(**{field: 36_501})


def test_http_tuning_max_retries_is_capped_at_ten() -> None:
    assert HttpTuning(max_retries=10).max_retries == 10
    with pytest.raises(ValidationError, match="max_retries"):
        HttpTuning(max_retries=11)


class _Store:
    """A store whose connect fails when asked to."""

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.closed = 0

    async def connect(self) -> None:
        if self.fails:
            raise ConnectionRefusedError("store unavailable")

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_connection_removal_failed_connect_closes_the_connected_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, vectors, graph = _Store(), _Store(fails=True), _Store()
    monkeypatch.setattr(connection_removal, "build_ingestion_control", lambda _: control)
    monkeypatch.setattr(connection_removal, "build_vector_repository", lambda _: vectors)
    monkeypatch.setattr(connection_removal, "build_knowledge_graph", lambda _: graph)
    removal = ConnectionRemoval(RuntimeSettings())

    with pytest.raises(ConnectionRefusedError):
        await removal.remove(tenant_id="acme", source_key="demo-tickets")

    assert (control.closed, vectors.closed, graph.closed) == (1, 0, 1)
    await removal.aclose()
    assert (control.closed, graph.closed) == (1, 1)


def test_secrets_key_with_surrounding_whitespace_is_kept_and_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = " demo-key-sentinel-0001\t"
    caplog.set_level(logging.WARNING, logger="harborrag.runtime.composition")

    used = secrets_encryption_key(RuntimeSettings(control_db_url=_DSN, secrets_encryption_key=key))

    assert used == key
    (record,) = caplog.records
    assert record.__dict__["event"] == "control_plane.secrets_key_whitespace"
    assert record.levelno == logging.WARNING
    assert "leading or trailing whitespace" in record.getMessage()
    assert "dropped_fields" not in record.__dict__
    assert "demo-key-sentinel-0001" not in caplog.text


def test_secrets_key_without_whitespace_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="harborrag.runtime.composition")

    used = secrets_encryption_key(
        RuntimeSettings(control_db_url=_DSN, secrets_encryption_key="demo-key-sentinel-0001")
    )

    assert used == "demo-key-sentinel-0001"
    assert caplog.text == ""
