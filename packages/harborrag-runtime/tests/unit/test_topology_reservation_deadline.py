import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_adapters.repositories.object_store import (
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    MemoryObjectStore,
)
from harborrag_core.topology.budget import BudgetAdmission, BudgetReservation
from harborrag_core.topology.derived import DescriptionPacket
from harborrag_runtime.topology.derived_models import (
    DerivedBudget,
    DescriptionArtifacts,
    FrozenDescriptionGenerator,
)
from harborrag_runtime.topology.reservation_deadline import reservation_seconds


def admission(seconds):
    return BudgetAdmission(
        admitted=True,
        reservation=BudgetReservation(
            reservation_id="reservation",
            tenant_id="tenant",
            job_id="job",
            fence=1,
            reserved_tokens=100,
            reserved_cost_usd=Decimal("0.1"),
            expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        ),
    )


def test_deadline_leaves_cancellation_margin_and_rejects_expired_or_missing_slots():
    assert 0 < reservation_seconds(admission(2)) < 2
    with pytest.raises(TimeoutError, match="expired"):
        reservation_seconds(admission(-1))
    with pytest.raises(ValueError, match="durable reservation"):
        reservation_seconds(BudgetAdmission(admitted=True))


@pytest.mark.asyncio
async def test_llm_summary_is_cancelled_before_reservation_expires_and_charge_is_retained(
    monkeypatch,
):
    monkeypatch.setattr(
        "harborrag_runtime.topology.derived_models.reservation_seconds", lambda value: 0.01
    )
    repository = SimpleNamespace(
        reserve_for_build=AsyncMock(return_value=admission(30)), settle_budget=AsyncMock()
    )
    entered = asyncio.Event()

    async def slow(packets):
        entered.set()
        await asyncio.Event().wait()

    store = MemoryObjectStore()
    await store.connect()
    wrapped = FrozenDescriptionGenerator(
        SimpleNamespace(generate=slow),
        DerivedBudget(repository, "tenant", "build", Decimal("0.1")),
        DescriptionArtifacts(
            ImmutableArtifactReader(store), ImmutableArtifactWriter(store), "profile"
        ),
        128,
    )
    try:
        with pytest.raises(TimeoutError):
            await wrapped.generate(
                (DescriptionPacket(packet_id="packet", text="Source.", chunk_ids=("chunk",)),)
            )
        assert entered.is_set()
        repository.settle_budget.assert_awaited_once()
        assert repository.settle_budget.await_args.args[2].input_tokens is None
    finally:
        await store.close()
