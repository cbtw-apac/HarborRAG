from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_core.models.chat import HarborToolCall

from .normalization import normalize_tool_calls


@dataclass(slots=True)
class ToolCallFragmentBuffer:
    """Accumulate identity, name, and argument fragments for one tool call index."""

    index: int
    call_id: str = ""
    call_type: str = "function"
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)

    def add(self, delta: HarborToolCall) -> None:
        """Append one normalized fragment while retaining stable identity fields."""

        if delta.id and not self.call_id:
            self.call_id = delta.id
        if delta.type:
            self.call_type = delta.type
        if delta.function.name:
            self.name_parts.append(delta.function.name)
        if delta.function.arguments:
            self.argument_parts.append(delta.function.arguments)

    def build(self) -> HarborToolCall:
        """Build one complete call and parse its assembled JSON arguments."""

        return normalize_tool_calls(
            [
                {
                    "id": self.call_id,
                    "type": self.call_type,
                    "index": self.index,
                    "function": {
                        "name": "".join(self.name_parts),
                        "arguments": "".join(self.argument_parts),
                    },
                }
            ]
        )[0]


class StreamingToolCallAssembler:
    """Assemble interleaved parallel tool-call deltas by provider call index."""

    def __init__(self) -> None:
        """Initialize an empty set of indexed tool-call buffers."""

        self._buffers: dict[int, ToolCallFragmentBuffer] = {}

    def add(self, delta: HarborToolCall, *, fallback_index: int) -> HarborToolCall:
        """Add one fragment and return the normalized caller-visible delta."""

        index = delta.index if delta.index is not None else fallback_index
        buffer = self._buffers.setdefault(index, ToolCallFragmentBuffer(index=index))
        buffer.add(delta)
        return delta

    def completed_calls(self) -> tuple[HarborToolCall, ...]:
        """Return all complete calls ordered by their provider-assigned index."""

        return tuple(self._buffers[index].build() for index in sorted(self._buffers))

    @property
    def pending_count(self) -> int:
        """Return the number of tool calls currently being assembled."""

        return len(self._buffers)
