from __future__ import annotations

from collections.abc import Mapping

from harborrag_core.contracts.chunking import SourceSpan


def structural_span(source_span: SourceSpan | None) -> SourceSpan | None:
    """Retain coarse provenance when a provider transforms source markup."""

    if source_span is None:
        return None
    return SourceSpan(
        page_start=source_span.page_start,
        page_end=source_span.page_end,
        element_ids=source_span.element_ids,
    )


def metadata_path(metadata: object, ordered_keys: tuple[str, ...]) -> tuple[str, ...]:
    """Read a deterministic non-empty structural path from provider metadata."""

    if not isinstance(metadata, Mapping):
        return ()
    return tuple(
        str(metadata[key]).strip()
        for key in ordered_keys
        if key in metadata and str(metadata[key]).strip()
    )
