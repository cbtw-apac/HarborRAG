"""Deterministic quality policy applied after provider schema validation."""

from __future__ import annotations

from harborrag_core.topology import (
    ChunkExtractionInput,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionOutput,
)
from harborrag_core.topology.text_policy import (
    CHUNK_DESCRIPTION_MAX_WORDS,
    CHUNK_TITLE_MAX_WORDS,
    ENTITY_DESCRIPTION_MAX_WORDS,
    RELATION_DESCRIPTION_MAX_WORDS,
    enforce_entity_name,
    enforce_text_budget,
)

OUTPUT_POLICY_VERSION = "bounded-graph-text-v2-reject"


class OutputPolicyViolation(ValueError):
    """A complete model response contained candidates HarborRAG cannot accept."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__(
            f"semantic output rejected ({len(reasons)} violation(s)): " + ", ".join(reasons)
        )


class ExtractionOutputPolicy:
    """Validate graph text atomically; never truncate or silently discard candidates."""

    def apply(self, output: ExtractionOutput, value: ChunkExtractionInput) -> ExtractionOutput:
        valid_entities, reasons = self._entities(output)
        assertions, assertion_reasons = self._assertions(output, valid_entities)
        reasons.extend(assertion_reasons)
        title, description, chunk_reasons = self._chunk_text(output, value)
        reasons.extend(chunk_reasons)
        if reasons:
            raise OutputPolicyViolation(tuple(reasons))
        payload = output.model_dump()
        payload.update(
            entities=valid_entities,
            assertions=assertions,
            title=title,
            description=description,
        )
        return ExtractionOutput.model_validate(payload)

    @staticmethod
    def _entities(
        output: ExtractionOutput,
    ) -> tuple[tuple[ExtractedEntity, ...], list[str]]:
        valid_entities = []
        reasons: list[str] = []
        for entity in output.entities:
            try:
                name = enforce_entity_name(entity.name)
                if (
                    entity.external_id
                    and entity.external_id.strip().casefold() not in entity.span.quote.casefold()
                ):
                    raise ValueError("external identity is not grounded in the entity span")
                description = enforce_text_budget(
                    entity.description,
                    field="entity description",
                    max_words=ENTITY_DESCRIPTION_MAX_WORDS,
                )
            except ValueError as error:
                reasons.append(f"entity:{entity.local_id}:{error}")
                continue
            aliases: list[str] = []
            for alias in entity.aliases:
                if alias == name:
                    continue
                try:
                    aliases.append(enforce_entity_name(alias))
                except ValueError as error:
                    reasons.append(f"entity:{entity.local_id}:alias:{error}")
            valid_entities.append(
                entity.model_copy(
                    update={
                        "name": name,
                        "description": description,
                        "aliases": tuple(aliases),
                    }
                )
            )
        return tuple(valid_entities), reasons

    @staticmethod
    def _assertions(
        output: ExtractionOutput, valid_entities: tuple[ExtractedEntity, ...]
    ) -> tuple[tuple[ExtractedAssertion, ...], list[str]]:
        valid_ids = {entity.local_id for entity in valid_entities}
        assertions = []
        reasons: list[str] = []
        for assertion in output.assertions:
            if assertion.subject_id not in valid_ids or assertion.object_id not in valid_ids:
                reasons.append(f"assertion:{assertion.local_id}:invalid_endpoint")
                continue
            try:
                statement = enforce_text_budget(
                    assertion.statement_text,
                    field="relationship description",
                    max_words=RELATION_DESCRIPTION_MAX_WORDS,
                )
            except ValueError as error:
                reasons.append(f"assertion:{assertion.local_id}:{error}")
                continue
            assertions.append(assertion.model_copy(update={"statement_text": statement}))
        return tuple(assertions), reasons

    @classmethod
    def _chunk_text(
        cls, output: ExtractionOutput, value: ChunkExtractionInput
    ) -> tuple[str, str, list[str]]:
        reasons: list[str] = []
        try:
            title = cls._human_title(output.title, value)
            description = enforce_text_budget(
                output.description,
                field="chunk description",
                max_words=CHUNK_DESCRIPTION_MAX_WORDS,
            )
        except ValueError as error:
            reasons.append(f"chunk:{error}")
            title, description = output.title, output.description
        return title, description, reasons

    @classmethod
    def _human_title(cls, title: str, value: ChunkExtractionInput) -> str:
        stripped = enforce_text_budget(
            title,
            field="chunk title",
            max_words=CHUNK_TITLE_MAX_WORDS,
        )
        if any(character.isalpha() for character in stripped):
            return stripped
        candidates = (*reversed(value.heading_path), value.source_title, "Evidence chunk")
        for candidate in candidates:
            if not any(character.isalpha() for character in candidate):
                continue
            return enforce_text_budget(
                candidate,
                field="fallback chunk title",
                max_words=CHUNK_TITLE_MAX_WORDS,
            )
        raise ValueError("chunk title has no human-readable fallback")
