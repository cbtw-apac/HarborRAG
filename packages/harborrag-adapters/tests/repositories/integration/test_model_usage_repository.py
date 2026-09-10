"""Integration coverage for durable model-usage accounting."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.control_plane.usage import SqlModelUsageRepository
from harborrag_core.ports.usage import ModelUsageRecord, ModelUsageTotals

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _usage(user_id: str, *, tenant_id: str = "ACME", **overrides: Any) -> ModelUsageRecord:
    fields: dict[str, Any] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "principal_id": "shared-principal",
        "surface": "chat",
        "logical_model": "chat-default",
        "provider": "openai",
        "provider_model": "gpt-4.1-mini",
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
        "estimated_cost_usd": 0.5,
        "created_at": _NOW,
    }
    fields.update(overrides)
    return ModelUsageRecord(**fields)


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_usage_records_round_trip_and_aggregate_by_tenant_user_and_window(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repo = SqlModelUsageRepository(create_session_factory(engine))
    try:
        await repo.record(
            _usage(
                "user-1",
                session_id="session-1",
                run_id="run-1",
                surface="agent",
                finish_reason="stop",
            )
        )
        await repo.record(_usage("user-1", created_at=_NOW + timedelta(hours=1)))
        await repo.record(_usage("user-2", estimated_cost_usd=None))
        await repo.record(_usage("user-1", tenant_id="OTHER"))

        assert await repo.totals(tenant_id="ACME") == ModelUsageTotals(
            requests=3,
            prompt_tokens=30,
            completion_tokens=12,
            total_tokens=42,
            estimated_cost_usd=1.0,
        )
        assert await repo.totals(tenant_id="ACME", user_id="user-1") == ModelUsageTotals(
            requests=2,
            prompt_tokens=20,
            completion_tokens=8,
            total_tokens=28,
            estimated_cost_usd=1.0,
        )
        # A null cost contributes nothing rather than making the sum null.
        no_cost = await repo.totals(tenant_id="ACME", user_id="user-2")
        assert no_cost.requests == 1
        assert no_cost.estimated_cost_usd == 0.0
        windowed = await repo.totals(
            tenant_id="ACME", user_id="user-1", since=_NOW + timedelta(minutes=30)
        )
        assert windowed.requests == 1
        assert await repo.totals(tenant_id="UNKNOWN") == ModelUsageTotals()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_a_failed_commit_is_swallowed_rather_than_failing_the_answer(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A duplicate id fails at commit -- the realistic accounting failure."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    repo = SqlModelUsageRepository(create_session_factory(engine))
    usage = _usage("user-1", id="usage-fixed")
    try:
        await repo.record(usage)
        with caplog.at_level(logging.ERROR):
            assert await repo.record(usage) is None

        assert "Model usage record failed" in caplog.text
        assert "usage_id=usage-fixed" in caplog.text
        # The first record survived and still aggregates.
        assert (await repo.totals(tenant_id="ACME")).requests == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.blackbox
async def test_recording_usage_never_raises_into_the_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _explode() -> Any:
        raise RuntimeError("control database is down")

    broken = SimpleNamespace(begin=_explode)
    repo = SqlModelUsageRepository(cast(Any, broken))

    with caplog.at_level(logging.ERROR):
        await repo.record(_usage("user-1", session_id="session-1"))

    assert "Model usage record failed" in caplog.text
    # Identifiers only: no prompt, completion, or token payload in the log.
    assert "tenant_id=ACME" in caplog.text
    assert "user_id=user-1" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "gpt-4.1-mini" not in caplog.text
