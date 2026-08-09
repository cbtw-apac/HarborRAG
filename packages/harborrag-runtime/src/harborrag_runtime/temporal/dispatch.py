"""Durable document-dispatch counters shared by bounded workflows."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.ingestion import DocumentIngestionOutcome


@dataclass(frozen=True, slots=True)
class DocumentDispatchSummary:
    published: int = 0
    unchanged: int = 0
    failed: int = 0

    def __post_init__(self) -> None:
        if min(self.published, self.unchanged, self.failed) < 0:
            raise ValueError("document dispatch counts must not be negative")

    def add(self, outcome: DocumentIngestionOutcome) -> DocumentDispatchSummary:
        if not isinstance(outcome, DocumentIngestionOutcome):
            raise ValueError(f"unsupported document ingestion outcome: {outcome!r}")
        return DocumentDispatchSummary(
            published=self.published + (outcome is DocumentIngestionOutcome.PUBLISHED),
            unchanged=self.unchanged + (outcome is DocumentIngestionOutcome.UNCHANGED),
            failed=self.failed + (outcome is DocumentIngestionOutcome.FAILED),
        )

    @property
    def total(self) -> int:
        return self.published + self.unchanged + self.failed

    def merge(self, other: DocumentDispatchSummary) -> DocumentDispatchSummary:
        return DocumentDispatchSummary(
            published=self.published + other.published,
            unchanged=self.unchanged + other.unchanged,
            failed=self.failed + other.failed,
        )


__all__ = ["DocumentDispatchSummary"]
