from __future__ import annotations

from harborrag_core.contracts.chunking import TokenCounter

from ..config import ChunkingProfile
from ..schemas import ChunkCandidate
from .tokenpacker import TokenBudgetPacker


class CompatiblePeerMerger:
    """Merge a small trailing peer backward when every boundary permits it."""

    def __init__(self, token_counter: TokenCounter, packer: TokenBudgetPacker) -> None:
        self._token_counter = token_counter
        self._packer = packer

    def merge(
        self,
        chunks: tuple[ChunkCandidate, ...],
        profile: ChunkingProfile,
    ) -> tuple[ChunkCandidate, ...]:
        """Merge undersized trailing peers when boundaries and limits permit."""

        if not profile.merge_small_peers or len(chunks) < 2:
            return chunks

        values = list(chunks)
        index = len(values) - 1
        while index > 0:
            current = values[index]
            previous = values[index - 1]
            if current.token_count >= profile.minimum_tokens:
                index -= 1
                continue
            left_unit = previous.units[-1]
            right_unit = current.units[0]
            content = f"{previous.content}\n\n{current.content}"
            if (
                self._packer.compatible(left_unit, right_unit)
                and self._token_counter.count(content) <= profile.soft_maximum_tokens
            ):
                values[index - 1 : index + 1] = [
                    self._packer.build((*previous.units, *current.units))
                ]
            index -= 1
        return tuple(values)
