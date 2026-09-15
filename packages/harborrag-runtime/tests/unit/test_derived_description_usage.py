"""Parent-description reservations must settle against real provider usage."""

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


class _ReportingDescriptions:
    async def generate_usage(self, packets):
        del packets
        return DescriptionRun(
            output=DescriptionOutput(
                description="Conditional deployment.",
                cited_packet_ids=("packet",),
                complete=True,
            ),
            usage=HarborChatUsage(prompt_tokens=900, completion_tokens=300, total_tokens=1200),
            provider_calls=1,
        )


@pytest.mark.asyncio
async def test_reported_description_usage_settles_actuals_not_the_reserved_ceiling() -> None:
    store = MemoryObjectStore()
    await store.connect()
    repository = Budget()
    generator = FrozenDescriptionGenerator(
        _ReportingDescriptions(),
        DerivedBudget(repository, "tenant", "build", Decimal("0.1")),
        DescriptionArtifacts(
            ImmutableArtifactReader(store), ImmutableArtifactWriter(store), "profile"
        ),
    )
    packets = (
        DescriptionPacket(packet_id="packet", text="Deploy only if approved.", chunk_ids=("c",)),
    )

    await generator.generate(packets)

    settled = repository.settlements[0][1]
    assert settled.input_tokens == 900
    assert settled.output_tokens == 300
    assert repository.requests[0].input_tokens > 900
    await store.close()
