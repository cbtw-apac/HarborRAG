"""Deterministic processing windows preserve original chunk identity and offsets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import Field

from harborrag_core.base import StrictModel

from .extraction import (
    MAX_OUTPUT_ASSERTIONS,
    MAX_OUTPUT_ENTITIES,
    MAX_OUTPUT_EVIDENCE,
    MAX_OUTPUT_GAPS,
    MAX_OUTPUT_TEXT_CHARS,
    MAX_OUTPUT_TITLE_CHARS,
    ChunkExtractionInput,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionIncompleteError,
    ExtractionOutput,
    ExtractionProfile,
    digest,
)
from .text_policy import CHUNK_DESCRIPTION_MAX_WORDS


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


_TEXT_LIMITS = {
    "title": MAX_OUTPUT_TITLE_CHARS,
    "description": MAX_OUTPUT_TEXT_CHARS,
    "retrieval_context": MAX_OUTPUT_TEXT_CHARS,
}


def merge_window_outputs(
    original: ChunkExtractionInput,
    windows: tuple[ExtractionWindow, ...],
    outputs: Mapping[str, ExtractionOutput],
    *,
    profile: ExtractionProfile | None = None,
) -> ExtractionOutput:
    """Merge complete frozen window outputs into one output that always validates.

    Each window satisfies the single-output caps; their union need not -- eight
    windows of 40 entities is 320 against a cap of 100. Entities are deduplicated
    by name, type and external id, assertions by their semantic key, and every
    collection is then cut to its cap in window order, so the same windows always
    merge to the same output instead of raising. ``profile`` supplies the schema
    v4 description word budget, which a plain join of window descriptions breaks.
    """

    _validate_coverage(original, windows, outputs)
    if len(windows) == 1:
        result = outputs[windows[0].window_id]
        result.require_complete()
        result.validate_evidence(original)
        return result
    merger = _WindowMerger()
    for window in windows:
        output = outputs[window.window_id]
        output.require_complete()
        output.validate_evidence(window.input)
        merger.add(window, output)
    description_words = (
        CHUNK_DESCRIPTION_MAX_WORDS
        if profile is not None and profile.schema_version == "4"
        else None
    )
    result = ExtractionOutput.model_validate(
        {
            **merger.graph(),
            **_merged_text(windows, outputs, description_words=description_words),
            "ontology_gaps": tuple(
                dict.fromkeys(gap for w in windows for gap in outputs[w.window_id].ontology_gaps)
            )[:MAX_OUTPUT_GAPS],
        }
    )
    result.validate_evidence(original)
    return result


@dataclass
class _WindowMerger:
    """Accumulate window entities and assertions, deduplicated, in window order."""

    entities: dict[tuple[str, str, str | None], ExtractedEntity] = field(default_factory=dict)
    assertions: dict[tuple[object, ...], ExtractedAssertion] = field(default_factory=dict)

    def add(self, window: ExtractionWindow, output: ExtractionOutput) -> None:
        ids: dict[str, str] = {}
        for entity in output.entities:
            key = (entity.name.strip().casefold(), entity.entity_type, entity.external_id)
            kept = self.entities.setdefault(key, _shifted_entity(window, entity))
            ids[entity.local_id] = kept.local_id
        for assertion in output.assertions:
            shifted = _shifted_assertion(window, assertion, ids)
            if (
                shifted.subject_id == shifted.object_id
                and assertion.subject_id != assertion.object_id
            ):
                continue  # two distinct mentions collapsed into one entity
            self.assertions.setdefault(_assertion_key(shifted), shifted)

    def graph(self) -> dict[str, object]:
        entities = tuple(self.entities.values())[:MAX_OUTPUT_ENTITIES]
        kept = {item.local_id for item in entities}
        assertions = tuple(
            item
            for item in self.assertions.values()
            if item.subject_id in kept and item.object_id in kept
        )[:MAX_OUTPUT_ASSERTIONS]
        return {"entities": entities, "assertions": assertions}


def _shifted_entity(window: ExtractionWindow, item: ExtractedEntity) -> ExtractedEntity:
    return item.model_copy(
        update={
            "local_id": digest([window.window_id, item.local_id]),
            "span": _shift(item.span, window.start),
            "description_evidence": tuple(
                _shift(span, window.start) for span in item.description_evidence
            ),
        }
    )


def _shifted_assertion(
    window: ExtractionWindow, item: ExtractedAssertion, ids: Mapping[str, str]
) -> ExtractedAssertion:
    return item.model_copy(
        update={
            "local_id": digest([window.window_id, item.local_id]),
            "subject_id": ids[item.subject_id],
            "object_id": ids[item.object_id],
            "span": _shift(item.span, window.start),
        }
    )


def _assertion_key(item: ExtractedAssertion) -> tuple[object, ...]:
    return (
        item.subject_id,
        item.predicate,
        item.object_id,
        item.polarity,
        item.modality,
        item.time_qualifier,
        item.statement_text.strip().casefold(),
    )


def _merged_text(
    windows: tuple[ExtractionWindow, ...],
    outputs: Mapping[str, ExtractionOutput],
    *,
    description_words: int | None,
) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for name, max_chars in _TEXT_LIMITS.items():
        # A title identifies the chunk; descriptions/context account for windows in order.
        selected = windows[:1] if name == "title" else windows
        text, evidence = _bounded_text(
            tuple((window, outputs[window.window_id]) for window in selected),
            name,
            max_chars=max_chars,
            max_words=description_words if name == "description" else None,
        )
        metadata[name] = text
        metadata[name + "_evidence"] = evidence
    return metadata


def _bounded_text(
    window_outputs: tuple[tuple[ExtractionWindow, ExtractionOutput], ...],
    name: str,
    *,
    max_chars: int,
    max_words: int | None,
) -> tuple[str, tuple[EvidenceSpan, ...]]:
    """Join window texts in order while they fit; only included windows add evidence.

    The first non-empty text is always kept -- it already passed its own window's
    validation -- so a generated text never loses the grounding spans it requires.
    """

    parts: list[str] = []
    evidence: list[EvidenceSpan] = []
    for window, output in window_outputs:
        text = getattr(output, name)
        if not text or text in parts:
            continue
        joined = "\n".join([*parts, text])
        if parts and (
            len(joined) > max_chars or (max_words is not None and len(joined.split()) > max_words)
        ):
            break
        parts.append(text)
        evidence.extend(_shift(span, window.start) for span in getattr(output, name + "_evidence"))
    return "\n".join(parts), tuple(evidence[:MAX_OUTPUT_EVIDENCE])


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
