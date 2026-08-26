"""Source-agnostic routing for canonical document normalizers."""

from __future__ import annotations

from collections.abc import Mapping

from harborrag_core.domain import Document, ParsedDocument, RawDocument
from harborrag_engine.ingestion import BaseDocumentNormalizer


def source_system_key(value: object) -> str:
    """Return the canonical lookup key used by registration and routing."""

    return str(value or "").strip().casefold()


class SourceDocumentNormalizer(BaseDocumentNormalizer):
    """Route source-owned formats through an immutable provider map."""

    def __init__(
        self,
        *,
        default: BaseDocumentNormalizer,
        providers: Mapping[str, BaseDocumentNormalizer],
    ) -> None:
        self._default = default
        self._providers = {
            source_system_key(source_system): normalizer
            for source_system, normalizer in providers.items()
        }

    @property
    def source_systems(self) -> tuple[str, ...]:
        """Return registered source systems for diagnostics and tests."""

        return tuple(sorted(self._providers))

    def normalize(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
    ) -> Document:
        source_system = source_system_key(raw.metadata.get("source_system"))
        normalizer = self._providers.get(source_system, self._default)
        return normalizer.normalize(raw, parsed)
