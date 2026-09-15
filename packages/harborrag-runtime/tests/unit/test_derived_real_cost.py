"""Settled spend must reflect priced token usage, not the flat reservation ceiling."""

from __future__ import annotations

from decimal import Decimal

import pytest
from test_topology_derived import Budget, MemoryObjectStore

from harborrag_adapters.repositories.object_store import (
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_adapters.topology.descriptions import DescriptionRun
from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.topology.derived import DescriptionOutput, DescriptionPacket
from harborrag_runtime.topology.derived_models import (
    DerivedBudget,
    DescriptionArtifacts,
    FrozenDescriptionGenerator,
)

pytestmark = pytest.mark.unit


class _PricedDescriptions:
    async def generate_usage(self, packets):
        del packets
        return DescriptionRun(
            output=DescriptionOutput(
                description="Rolled up.", cited_packet_ids=("packet",), complete=True
            ),
            usage=HarborChatUsage(prompt_tokens=4000, completion_tokens=1000, total_tokens=5000),
            provider_calls=1,
            cost_usd=Decimal("0.027"),
        )


@pytest.mark.asyncio
async def test_priced_usage_settles_the_measured_cost() -> None:
    store = MemoryObjectStore()
    await store.connect()
    repository = Budget()
    generator = FrozenDescriptionGenerator(
        _PricedDescriptions(),
        DerivedBudget(repository, "tenant", "build", Decimal("0.1")),
        DescriptionArtifacts(
            ImmutableArtifactReader(store), ImmutableArtifactWriter(store), "profile"
        ),
    )

    await generator.generate(
        (DescriptionPacket(packet_id="packet", text="Deploy only if approved.", chunk_ids=("c",)),)
    )

    settled = repository.settlements[0][1]
    assert settled.cost_usd == Decimal("0.027")
    await store.close()


class _CountingDescriptions:
    def __init__(self, cost: Decimal) -> None:
        self.calls = 0
        self._cost = cost

    async def generate_usage(self, packets):
        self.calls += 1
        return DescriptionRun(
            output=DescriptionOutput(
                description=f"Rolled up {self.calls}.",
                cited_packet_ids=(packets[0].packet_id,),
                complete=True,
            ),
            usage=HarborChatUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            provider_calls=1,
            cost_usd=self._cost,
        )


@pytest.mark.asyncio
async def test_the_run_stops_once_its_usd_ceiling_is_reached() -> None:
    from harborrag_runtime.topology.budgeted_extractor import EnrichmentDeferredError
    from harborrag_runtime.topology.run_cost import RunCostLedger

    store = MemoryObjectStore()
    await store.connect()
    delegate = _CountingDescriptions(Decimal("3.00"))
    generator = FrozenDescriptionGenerator(
        delegate,
        DerivedBudget(Budget(), "tenant", "build", Decimal("10")),
        DescriptionArtifacts(
            ImmutableArtifactReader(store), ImmutableArtifactWriter(store), "profile"
        ),
        run_costs=RunCostLedger(ceiling_usd=Decimal("5.00")),
    )

    def packet(name: str):
        return (DescriptionPacket(packet_id=name, text=f"body {name}", chunk_ids=(name,)),)

    await generator.generate(packet("a"))
    await generator.generate(packet("b"))
    # 6.00 spent against a 5.00 ceiling: the next call must not be issued.
    with pytest.raises(EnrichmentDeferredError, match="run_budget_exhausted"):
        await generator.generate(packet("c"))

    assert delegate.calls == 2
    await store.close()
