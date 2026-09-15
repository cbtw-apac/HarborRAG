"""Plan and execute bounded bottom-up summaries over stable structure identities."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from harborrag_core.chunking.identity import encoded_identifier
from harborrag_core.ports.description_generation import DescriptionGeneratorPort
from harborrag_core.topology.derived import (
    ChunkEnrichment,
    DescriptionPacket,
    ParentDescription,
    description_prompt_json,
)
from harborrag_core.topology.extraction import digest
from harborrag_core.topology.text_policy import PARENT_DESCRIPTION_MAX_CHARS

PARENT_BUILDER_VERSION = "parent-builder-v2-stable-structure-preflight"


@dataclass(frozen=True)
class ParentDescriptionPolicy:
    max_fan_in: int = 8
    max_input_bytes: int = 24000
    max_input_tokens: int = 6000
    max_calls: int = 64
    token_counter_revision: str = "ascii-four-nonascii-one-v1"

    def __post_init__(self) -> None:
        if (
            self.max_fan_in < 2
            or self.max_input_bytes < 100
            or self.max_input_tokens < 100
            or self.max_calls < 1
            or not self.token_counter_revision.strip()
        ):
            raise ValueError("description reduction budgets must be positive and fan-in >=2")


@dataclass
class _CallBudget:
    remaining: int

    def reserve(self) -> None:
        if self.remaining < 1:
            raise ValueError("parent reduction call budget exhausted")
        self.remaining -= 1


@dataclass(frozen=True)
class _ParentSpec:
    key: str
    level: Literal["section", "document", "folder"]
    section_path: tuple[str, ...] = ()
    structure_id: str | None = None


class ParentDescriptionBuilder:
    def __init__(
        self,
        generator: DescriptionGeneratorPort,
        policy: ParentDescriptionPolicy = ParentDescriptionPolicy(),
    ) -> None:
        self._generator, self._policy = generator, policy

    async def build(
        self, document_id: str, chunks: tuple[ChunkEnrichment, ...]
    ) -> tuple[ParentDescription, ...]:
        if not chunks or any(not chunk.description.strip() for chunk in chunks):
            raise ValueError("parent description pending: child enrichment is incomplete")
        if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
            raise ValueError("parent description inputs contain duplicate chunk identities")
        budget = _CallBudget(self._policy.max_calls)
        direct: dict[tuple[str, ...], list[DescriptionPacket]] = defaultdict(list)
        paths: set[tuple[str, ...]] = set()
        labels: dict[tuple[str, ...], tuple[str, ...]] = {}
        for chunk in chunks:
            structure_path = self._structure_path(document_id, chunk)
            direct[structure_path].append(
                DescriptionPacket(
                    packet_id=chunk.chunk_id,
                    text=chunk.description,
                    chunk_ids=(chunk.chunk_id,),
                )
            )
            for depth in range(1, len(structure_path) + 1):
                prefix = structure_path[:depth]
                paths.add(prefix)
                label = chunk.section_path[:depth]
                existing = labels.setdefault(prefix, label)
                if existing != label:
                    raise ValueError("stable section identity maps to conflicting heading labels")
        required_calls = self._planned_required_calls(direct, paths)
        if required_calls > self._policy.max_calls:
            raise ValueError(
                "parent reduction plan requires "
                f"{required_calls} calls but the configured budget allows "
                f"{self._policy.max_calls}; no provider calls were issued"
            )
        parents: list[ParentDescription] = []
        reduced_sections: dict[tuple[str, ...], DescriptionPacket] = {}
        for path in sorted(paths, key=lambda item: (-len(item), item)):
            child_packets = [
                reduced_sections[child]
                for child in sorted(reduced_sections)
                if len(child) == len(path) + 1 and child[:-1] == path
            ]
            packets = (*direct.get(path, ()), *child_packets)
            parent_key = digest([document_id, "section", path[-1]])
            reduced = await self._reduce(packets, budget)
            reduced_sections[path] = reduced
            parents.append(
                self._parent(
                    _ParentSpec(parent_key, "section", labels[path], path[-1]),
                    packets,
                    reduced,
                )
            )
        document_packets = (
            *direct.get((), ()),
            *(reduced_sections[path] for path in sorted(reduced_sections) if len(path) == 1),
        )
        document = await self._reduce(document_packets, budget)
        parents.append(
            self._parent(_ParentSpec(document_id, "document"), document_packets, document)
        )
        return tuple(parents)

    @staticmethod
    def _structure_path(document_id: str, chunk: ChunkEnrichment) -> tuple[str, ...]:
        if chunk.section_ids:
            if len(chunk.section_ids) != len(chunk.section_path):
                raise ValueError("section identity path does not match heading path")
            return chunk.section_ids
        return tuple(
            encoded_identifier(
                "section",
                {"document_id": document_id, "section_path": chunk.section_path[:depth]},
            )
            for depth in range(1, len(chunk.section_path) + 1)
        )

    def _planned_required_calls(
        self,
        direct: dict[tuple[str, ...], list[DescriptionPacket]],
        paths: set[tuple[str, ...]],
    ) -> int:
        calls = 0
        reduced_sections: dict[tuple[str, ...], DescriptionPacket] = {}
        for path in sorted(paths, key=lambda item: (-len(item), item)):
            packets = (
                *direct.get(path, ()),
                *(
                    reduced_sections[child]
                    for child in sorted(reduced_sections)
                    if len(child) == len(path) + 1 and child[:-1] == path
                ),
            )
            reduced_sections[path], added = self._plan_reduce(packets)
            calls += added
        document_packets = (
            *direct.get((), ()),
            *(reduced_sections[path] for path in sorted(reduced_sections) if len(path) == 1),
        )
        _, added = self._plan_reduce(document_packets)
        calls += added
        return calls

    def _plan_reduce(self, packets: tuple[DescriptionPacket, ...]) -> tuple[DescriptionPacket, int]:
        current = packets
        calls = 0
        while len(current) > 1:
            next_level = []
            for group in self._groups(current):
                if len(group) == 1:
                    next_level.append(group[0])
                else:
                    calls += 1
                    next_level.append(self._planned_packet(group))
            if len(next_level) >= len(current):
                raise ValueError(
                    "parent reduction plan cannot fit two packets; no provider calls were issued"
                )
            current = tuple(next_level)
        return current[0], calls

    @staticmethod
    def _planned_packet(packets: tuple[DescriptionPacket, ...]) -> DescriptionPacket:
        return DescriptionPacket(
            packet_id=digest([packet.packet_id for packet in packets]),
            # A non-ASCII character is the estimator's worst case: one token
            # and up to three UTF-8 bytes per permitted output character.
            text="界" * PARENT_DESCRIPTION_MAX_CHARS,
            chunk_ids=tuple(
                dict.fromkeys(chunk for packet in packets for chunk in packet.chunk_ids)
            ),
        )

    async def _reduce(
        self, packets: tuple[DescriptionPacket, ...], budget: _CallBudget
    ) -> DescriptionPacket:
        current = packets
        while len(current) > 1:
            next_level = tuple(
                [await self._summarize(group, budget) for group in self._groups(current)]
            )
            if len(next_level) >= len(current):
                raise ValueError("parent reduction cannot fit two children; increase input budget")
            current = next_level
        return current[0]

    def _groups(
        self, packets: tuple[DescriptionPacket, ...]
    ) -> tuple[tuple[DescriptionPacket, ...], ...]:
        groups: list[tuple[DescriptionPacket, ...]] = []
        group: list[DescriptionPacket] = []
        for packet in packets:
            if not self._fits((packet,)):
                raise ValueError("parent packet exceeds input budget; cannot silently truncate")
            if group and (
                len(group) == self._policy.max_fan_in or not self._fits((*group, packet))
            ):
                groups.append(tuple(group))
                group = []
            group.append(packet)
        if group:
            groups.append(tuple(group))
        return tuple(groups)

    def _fits(self, packets: tuple[DescriptionPacket, ...]) -> bool:
        payload = description_prompt_json(packets)
        return (
            len(payload.encode()) <= self._policy.max_input_bytes
            and _estimated_tokens(payload) <= self._policy.max_input_tokens
        )

    async def _summarize(
        self, packets: tuple[DescriptionPacket, ...], budget: _CallBudget
    ) -> DescriptionPacket:
        if len(packets) == 1:
            return packets[0]
        budget.reserve()
        output = await self._generator.generate(packets)
        if not output.complete or not set(output.cited_packet_ids) <= {
            packet.packet_id for packet in packets
        }:
            raise ValueError("parent description is incomplete or cites unknown packets")
        output.require_bounded_description()
        # All inputs determine visibility, including inputs not cited by the model.
        return DescriptionPacket(
            packet_id=digest([packet.model_dump(mode="json") for packet in packets]),
            text=output.description,
            chunk_ids=tuple(
                dict.fromkeys(chunk for packet in packets for chunk in packet.chunk_ids)
            ),
            cited_chunk_ids=tuple(
                dict.fromkeys(
                    chunk
                    for packet in packets
                    if packet.packet_id in output.cited_packet_ids
                    for chunk in (packet.cited_chunk_ids or packet.chunk_ids)
                )
            ),
        )

    @staticmethod
    def _parent(
        spec: _ParentSpec,
        inputs: tuple[DescriptionPacket, ...],
        reduced: DescriptionPacket,
    ) -> ParentDescription:
        chunk_ids = tuple(dict.fromkeys(chunk for packet in inputs for chunk in packet.chunk_ids))
        return ParentDescription(
            parent_key=spec.key,
            level=spec.level,
            section_path=spec.section_path,
            structure_id=spec.structure_id,
            description=reduced.text,
            input_chunk_ids=chunk_ids,
            cited_chunk_ids=reduced.cited_chunk_ids or reduced.chunk_ids,
            input_digest=digest([packet.model_dump(mode="json") for packet in inputs]),
        )


def _estimated_tokens(value: str) -> int:
    """Conservative provider-neutral estimate; revision is frozen in the policy."""

    ascii_characters = sum(ord(character) < 128 for character in value)
    non_ascii_characters = len(value) - ascii_characters
    return max(1, (ascii_characters + 3) // 4 + non_ascii_characters)
