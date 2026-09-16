"""Bounded, cached reductions with exact model-input keys and local packet IDs."""

import json
from dataclasses import dataclass

from harborrag_core.ports.description_generation import DescriptionGeneratorPort
from harborrag_core.ports.summary_projection import SummaryCachePort
from harborrag_core.summaries import SummaryCard, SummaryPolicy, generation_key
from harborrag_core.topology.derived import DescriptionPacket, description_prompt_json
from harborrag_core.topology.extraction import digest


class SummaryBudgetDeferred(ValueError):
    """Completed reductions remain reusable when this run exhausts its allowance."""


@dataclass
class SummaryReducer:
    tenant_id: str
    policy: SummaryPolicy
    cache: SummaryCachePort
    generator: DescriptionGeneratorPort
    calls: int = 0

    def _packets(self, inputs: tuple[str, ...]) -> tuple[DescriptionPacket, ...]:
        return tuple(
            DescriptionPacket(packet_id=f"p{index}", text=value, chunk_ids=(f"p{index}",))
            for index, value in enumerate(inputs)
        )

    def _fits(self, inputs: tuple[str, ...]) -> bool:
        if not inputs or any(len(value) > 24000 for value in inputs):
            return False
        payload = description_prompt_json(self._packets(inputs))
        ascii_count = sum(ord(value) < 128 for value in payload)
        tokens = (ascii_count + 3) // 4 + len(payload) - ascii_count
        return (
            len(inputs) <= self.policy.max_fan_in
            and len(payload.encode()) <= self.policy.max_input_bytes
            and tokens <= self.policy.max_input_tokens
        )

    def _split(self, text: str) -> tuple[str, ...]:
        """Split oversized canonical text without losing or silently truncating bytes."""
        parts = []
        while text:
            low, high = 1, min(len(text), 24000)
            if not self._fits((text[:1],)):
                raise ValueError("summary budget cannot fit one character")
            while low < high:
                middle = (low + high + 1) // 2
                if self._fits((text[:middle],)):
                    low = middle
                else:
                    high = middle - 1
            parts.append(text[:low])
            text = text[low:]
        return tuple(parts)

    async def reduce(
        self, kind: str, metadata: dict[str, object], inputs: tuple[str, ...]
    ) -> tuple[SummaryCard, str]:
        context = json.dumps(
            {"kind": kind, **metadata}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if not inputs:
            card = SummaryCard(description="No published content is available for this node.")
            key = generation_key(self.tenant_id, self.policy, [context, []])
            return await self.cache.put_card(self.tenant_id, key, card), key
        # The final node key includes context. Intermediate content reductions can
        # be reused across node and version identities within the tenant.
        key = generation_key(self.tenant_id, self.policy, [context, inputs])
        cached = await self.cache.get_card(self.tenant_id, key)
        if cached is not None:
            return cached, key
        current = tuple(part for value in inputs for part in self._split(value))
        while not self._fits((context, *current)):
            groups: list[tuple[str, ...]] = []
            group: tuple[str, ...] = ()
            for value in current:
                if group and not self._fits((*group, value)):
                    groups.append(group)
                    group = ()
                group = (*group, value)
            if group:
                groups.append(group)
            next_values = tuple([(await self._call(group)).model_dump_json() for group in groups])
            if len(next_values) >= len(current) and sum(map(len, next_values)) >= sum(
                map(len, current)
            ):
                raise ValueError("summary reduction cannot make progress within input budget")
            current = next_values
        card = await self._call((context, *current))
        return await self.cache.put_card(self.tenant_id, key, card), key

    async def _call(self, inputs: tuple[str, ...]) -> SummaryCard:
        key = generation_key(self.tenant_id, self.policy, ["reduction", inputs])
        cached = await self.cache.get_card(self.tenant_id, key)
        if cached is not None:
            return cached
        if self.calls >= self.policy.max_calls:
            raise SummaryBudgetDeferred("summary_call_budget")
        if not self._fits(inputs):
            raise ValueError("summary input exceeds configured budget")
        self.calls += 1
        packets = self._packets(inputs)
        output = await self.generator.generate(packets)
        if not output.complete or not set(output.cited_packet_ids) <= {
            p.packet_id for p in packets
        }:
            raise ValueError("summary output is incomplete or cites unknown input")
        card = SummaryCard(
            description=output.description,
            topics=output.topics,
            key_entities=output.key_entities,
            content_types=output.content_types,
        )
        return await self.cache.put_card(self.tenant_id, key, card)


def input_digest(metadata: dict[str, object], inputs: tuple[str, ...]) -> str:
    return digest([metadata, inputs])
