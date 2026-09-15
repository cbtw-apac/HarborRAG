"""Deterministic repair for exact model-provided evidence quotes."""

from harborrag_core.topology import ChunkExtractionInput, EvidenceSpan, ExtractionOutput


class EvidenceCanonicalizer:
    """Align exact model quotes without synthesizing or modifying evidence text.

    A unique quote is authoritative. When it repeats, the model's proposed start
    disambiguates only if one occurrence is strictly nearest; ties and absent
    quotes remain invalid and therefore fail closed.
    """

    def canonicalize(
        self, result: ExtractionOutput, value: ChunkExtractionInput
    ) -> ExtractionOutput:
        entities = tuple(
            item.model_copy(
                update={
                    "span": self._span(item.span, value),
                    "description_evidence": tuple(
                        self._span(span, value) for span in item.description_evidence
                    ),
                }
            )
            for item in result.entities
        )
        assertions = tuple(
            item.model_copy(update={"span": self._span(item.span, value)})
            for item in result.assertions
        )
        evidence = {
            name: tuple(self._span(span, value) for span in getattr(result, name))
            for name in (
                "title_evidence",
                "description_evidence",
                "retrieval_context_evidence",
            )
        }
        return result.model_copy(
            update={"entities": entities, "assertions": assertions, **evidence}
        )

    @staticmethod
    def _span(span: EvidenceSpan, value: ChunkExtractionInput) -> EvidenceSpan:
        content = getattr(value, span.source)
        if (
            span.end > span.start
            and span.end <= len(content)
            and content[span.start : span.end] == span.quote
        ):
            return span
        starts: list[int] = []
        cursor = 0
        while (start := content.find(span.quote, cursor)) >= 0:
            starts.append(start)
            cursor = start + 1
        if not starts:
            return span
        distance = min(abs(start - span.start) for start in starts)
        nearest = [start for start in starts if abs(start - span.start) == distance]
        if len(nearest) != 1:
            return span
        start = nearest[0]
        return span.model_copy(update={"start": start, "end": start + len(span.quote)})
