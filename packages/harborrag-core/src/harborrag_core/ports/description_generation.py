"""Bounded hierarchy description generation over identified evidence packets."""

from typing import Protocol

from harborrag_core.topology.derived import DescriptionOutput, DescriptionPacket


class DescriptionGeneratorPort(Protocol):
    async def generate(self, packets: tuple[DescriptionPacket, ...]) -> DescriptionOutput: ...
