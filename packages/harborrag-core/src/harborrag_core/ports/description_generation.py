"""Bounded hierarchy description generation over identified evidence packets."""

from decimal import Decimal
from typing import Protocol

from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.topology.derived import DescriptionOutput, DescriptionPacket


class DescriptionGeneratorPort(Protocol):
    async def generate(self, packets: tuple[DescriptionPacket, ...]) -> DescriptionOutput: ...


class DescriptionRunProtocol(Protocol):
    @property
    def output(self) -> DescriptionOutput: ...

    @property
    def usage(self) -> HarborChatUsage: ...

    @property
    def provider_calls(self) -> int: ...

    @property
    def cost_usd(self) -> Decimal | None: ...


class UsageAwareDescriptionPort(DescriptionGeneratorPort, Protocol):
    """Description generation that reports usage so reservations settle actuals."""

    async def generate_usage(
        self, packets: tuple[DescriptionPacket, ...]
    ) -> DescriptionRunProtocol: ...
