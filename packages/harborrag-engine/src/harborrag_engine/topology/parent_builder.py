"""Plan and execute bounded bottom-up summaries over stable structure identities."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from harborrag_core.chunking.identity import encoded_identifier
from harborrag_core.ports.description_generation import DescriptionGeneratorPort
from harborrag_core.topology.derived import (
    DescriptionPacket,
    ParentDescription,
    RollupSource,
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


@dataclass(frozen=True)
class _ReductionPlan:
    """Group sizes for each reduction level, planned against worst-case output."""

    levels: tuple[tuple[int, ...], ...]
    calls: int


@dataclass(frozen=True)
class _SectionPlan:
    path: tuple[str, ...]
    direct: tuple[DescriptionPacket, ...]
    children: tuple[tuple[str, ...], ...]
    reduction: _ReductionPlan


@dataclass(frozen=True)
class _ParentBuildPlan:
    sections: tuple[_SectionPlan, ...]
    document_direct: tuple[DescriptionPacket, ...]
    document_children: tuple[tuple[str, ...], ...]
    document_reduction: _ReductionPlan

    @property
    def calls(self) -> int:
        return self.document_reduction.calls + sum(
            section.reduction.calls for section in self.sections
        )


class ParentDescriptionBuilder:
    def __init__(
        self,
        generator: DescriptionGeneratorPort,
        policy: ParentDescriptionPolicy = ParentDescriptionPolicy(),
    ) -> None:
        self._generator, self._policy = generator, policy

    async def build(
        self, document_id: str, chunks: tuple[RollupSource, ...]
    ) -> tuple[ParentDescription, ...]:
        if not chunks or any(not chunk.text.strip() for chunk in chunks):
            raise ValueError("parent rollup pending: child text is incomplete")
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
                    text=chunk.text,
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
        plan = self._plan(direct, paths)
        if plan.calls > self._policy.max_calls:
            raise ValueError(
                "parent reduction plan requires "
                f"{plan.calls} calls but the configured budget allows "
                f"{self._policy.max_calls}; no provider calls were issued"
            )
        parents: list[ParentDescription] = []
        reduced_sections: dict[tuple[str, ...], DescriptionPacket] = {}
        for section in plan.sections:
            packets = (
                *section.direct,
                *(reduced_sections[child] for child in section.children),
            )
            parent_key = digest([document_id, "section", section.path[-1]])
            reduced = await self._execute(packets, section.reduction, budget)
            reduced_sections[section.path] = reduced
            parents.append(
                self._parent(
                    _ParentSpec(
                        parent_key,
                        "section",
                        labels[section.path],
                        section.path[-1],
                    ),
                    packets,
                    reduced,
                )
            )
        document_packets = (
            *plan.document_direct,
            *(reduced_sections[path] for path in plan.document_children),
        )
        document = await self._execute(document_packets, plan.document_reduction, budget)
        parents.append(
            self._parent(_ParentSpec(document_id, "document"), document_packets, document)
        )
        return tuple(parents)

    @staticmethod
    def _structure_path(document_id: str, chunk: RollupSource) -> tuple[str, ...]:
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

    def _plan(
        self,
        direct: dict[tuple[str, ...], list[DescriptionPacket]],
        paths: set[tuple[str, ...]],
    ) -> _ParentBuildPlan:
        sections: list[_SectionPlan] = []
        reduced_sections: dict[tuple[str, ...], DescriptionPacket] = {}
        for path in sorted(paths, key=lambda item: (-len(item), item)):
            children = tuple(
                child
                for child in sorted(reduced_sections)
                if len(child) == len(path) + 1 and child[:-1] == path
            )
            packets = (
                *direct.get(path, ()),
                *(reduced_sections[child] for child in children),
            )
            reduction, reduced_sections[path] = self._plan_reduction(packets)
            sections.append(_SectionPlan(path, tuple(direct.get(path, ())), children, reduction))
        document_children = tuple(path for path in sorted(reduced_sections) if len(path) == 1)
        document_packets = (
            *direct.get((), ()),
            *(reduced_sections[path] for path in document_children),
        )
        document_reduction, _ = self._plan_reduction(document_packets)
        return _ParentBuildPlan(
            tuple(sections),
            tuple(direct.get((), ())),
            document_children,
            document_reduction,
        )

    def _plan_reduction(
        self, packets: tuple[DescriptionPacket, ...]
    ) -> tuple[_ReductionPlan, DescriptionPacket]:
        current = packets
        calls = 0
        levels: list[tuple[int, ...]] = []
        while len(current) > 1:
            next_level = []
            groups = self._groups(current)
            levels.append(tuple(len(group) for group in groups))
            for group in groups:
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
        return _ReductionPlan(tuple(levels), calls), current[0]

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

    async def _execute(
        self,
        packets: tuple[DescriptionPacket, ...],
        plan: _ReductionPlan,
        budget: _CallBudget,
    ) -> DescriptionPacket:
        current = packets
        for sizes in plan.levels:
            groups: list[tuple[DescriptionPacket, ...]] = []
            offset = 0
            for size in sizes:
                groups.append(current[offset : offset + size])
                offset += size
            if offset != len(current):
                raise RuntimeError("parent reduction plan does not match its inputs")
            current = tuple([await self._summarize(group, budget) for group in groups])
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
