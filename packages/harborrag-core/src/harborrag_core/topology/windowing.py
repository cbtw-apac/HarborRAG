"""Deterministic processing windows preserve original chunk identity and offsets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import Field

from harborrag_core.base import StrictModel

from .extraction import (
    ChunkExtractionInput,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionIncompleteError,
    ExtractionOutput,
    ExtractionProfile,
    digest,
)


class ExtractionWindow(StrictModel):
    window_id: str
    chunk_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    input: ChunkExtractionInput


def input_character_count(value: ChunkExtractionInput) -> int:
    return (
        len(value.content)
        + len(value.context)
        + len(value.source_title)
        + sum(map(len, value.heading_path))
    )


def extraction_windows(
    value: ChunkExtractionInput, profile: ExtractionProfile
) -> tuple[ExtractionWindow, ...]:
    """Compatibility factory for the immutable extraction-window builder."""
    return ExtractionWindowBuilder(profile).build(value)


@dataclass(frozen=True)
class ExtractionWindowBuilder:
    """Cover every Unicode character exactly once, bounded by the declared call cap.

    Character budgets are explicit, not a claim of measured provider token usage.
    No overlap or additional context is invented; callers supply authorized context.
    """

    profile: ExtractionProfile

    def build(self, value: ChunkExtractionInput) -> tuple[ExtractionWindow, ...]:
        capacity = self.profile.max_input_chars - (
            input_character_count(value) - len(value.content)
        )
        if capacity <= 0:
            raise ExtractionIncompleteError("extraction context exhausts input window budget")
        count = max(1, (len(value.content) + capacity - 1) // capacity)
        if count > self.profile.max_windows:
            raise ExtractionIncompleteError(
                "extraction window coverage exceeds call budget; input is incomplete"
            )
        return tuple(
            self._window(value, start, min(len(value.content), start + capacity))
            for start in range(0, max(1, len(value.content)), capacity)
        )

    @staticmethod
    def _window(value: ChunkExtractionInput, start: int, end: int) -> ExtractionWindow:
        supplied = value.model_copy(
            update={"content": value.content[start:end], "window_start": value.window_start + start}
        )
        return ExtractionWindow(
            window_id=digest({"input": supplied.input_digest, "start": start, "end": end}),
            chunk_id=value.chunk_id,
            start=start,
            end=end,
            input=supplied,
        )


def _shift(span: EvidenceSpan, offset: int) -> EvidenceSpan:
    if span.source != "content":
        return span
    return span.model_copy(update={"start": span.start + offset, "end": span.end + offset})


def merge_window_outputs(
    original: ChunkExtractionInput,
    windows: tuple[ExtractionWindow, ...],
    outputs: Mapping[str, ExtractionOutput],
) -> ExtractionOutput:
    """Merge complete frozen window outputs; model IDs cannot collide across windows."""
    _validate_coverage(original, windows, outputs)
    if len(windows) == 1:
        result = outputs[windows[0].window_id]
        result.require_complete()
        result.validate_evidence(original)
        return result
    entities: list[ExtractedEntity] = []
    assertions: list[ExtractedAssertion] = []
    metadata: dict[str, object] = {}
    for window in windows:
        output = outputs[window.window_id]
        output.require_complete()
        output.validate_evidence(window.input)
        ids = {item.local_id: digest([window.window_id, item.local_id]) for item in output.entities}
        entities.extend(
            item.model_copy(
                update={"local_id": ids[item.local_id], "span": _shift(item.span, window.start)}
            )
            for item in output.entities
        )
        assertions.extend(
            item.model_copy(
                update={
                    "local_id": digest([window.window_id, item.local_id]),
                    "subject_id": ids[item.subject_id],
                    "object_id": ids[item.object_id],
                    "span": _shift(item.span, window.start),
                }
            )
            for item in output.assertions
        )
    for name in ("title", "description", "retrieval_context"):
        # A title identifies the chunk; descriptions/context account for all windows.
        selected = windows[:1] if name == "title" else windows
        metadata[name] = "\n".join(
            dict.fromkeys(
                getattr(outputs[w.window_id], name)
                for w in selected
                if getattr(outputs[w.window_id], name)
            )
        )
        metadata[name + "_evidence"] = tuple(
            _shift(span, w.start)
            for w in selected
            for span in getattr(outputs[w.window_id], name + "_evidence")
        )
    result = ExtractionOutput.model_validate(
        {
            "entities": entities,
            "assertions": assertions,
            **metadata,
            "ontology_gaps": tuple(
                dict.fromkeys(gap for w in windows for gap in outputs[w.window_id].ontology_gaps)
            ),
        }
    )
    result.validate_evidence(original)
    return result


def _validate_coverage(
    original: ChunkExtractionInput,
    windows: tuple[ExtractionWindow, ...],
    outputs: Mapping[str, ExtractionOutput],
) -> None:
    if not windows or set(outputs) != {window.window_id for window in windows}:
        raise ValueError("extraction window outputs do not cover the exact input plan")
    cursor = 0
    for window in windows:
        expected = original.model_copy(
            update={
                "content": original.content[window.start : window.end],
                "window_start": original.window_start + window.start,
            }
        )
        if (
            window.start != cursor
            or window.end < window.start
            or window.end > len(original.content)
            or window.chunk_id != original.chunk_id
            or window.input != expected
        ):
            raise ValueError("extraction windows changed source identity, context, or coverage")
        cursor = window.end
    if cursor != len(original.content):
        raise ValueError("extraction windows leave uncovered source content")
