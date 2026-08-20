"""Shared ingestion bounds used at public, application, and workflow boundaries."""

from __future__ import annotations

MAX_BATCH_SIZE = 300
MAX_CONTINUE_AFTER_BATCHES = 100
MAX_DISCOVERY_CONCURRENCY = 32
MAX_DISCOVERY_PAGE_SIZE = 300
MAX_DOCUMENT_CONCURRENCY = 100
MAX_REINDEX_LIMIT = 100_000
MAX_RETRY_DOCUMENT_IDS = 1_000


def validate_document_concurrency(value: int) -> None:
    if not 1 <= value <= MAX_DOCUMENT_CONCURRENCY:
        raise ValueError("document_concurrency must be between 1 and 100")


def validate_discovery_page_size(value: int) -> None:
    if not 1 <= value <= MAX_DISCOVERY_PAGE_SIZE:
        raise ValueError("discovery_page_size must be between 1 and 300")


def validate_discovery_concurrency(value: int) -> None:
    if not 1 <= value <= MAX_DISCOVERY_CONCURRENCY:
        raise ValueError("discovery_concurrency must be between 1 and 32")


def validate_source_orchestration_limits(
    *,
    batch_size: int,
    continue_after_batches: int,
) -> None:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("source batch_size must be between 1 and 300")
    if not 1 <= continue_after_batches <= MAX_CONTINUE_AFTER_BATCHES:
        raise ValueError("continue_after_batches must be between 1 and 100")


def validate_reindex_limit(value: int) -> None:
    if not 1 <= value <= MAX_REINDEX_LIMIT:
        raise ValueError("reindex limit must be between 1 and 100000")


__all__ = [
    "MAX_RETRY_DOCUMENT_IDS",
    "validate_discovery_concurrency",
    "validate_discovery_page_size",
    "validate_document_concurrency",
    "validate_reindex_limit",
    "validate_source_orchestration_limits",
]
