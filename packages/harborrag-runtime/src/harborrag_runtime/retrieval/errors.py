"""Translate storage faults into conditions a retrieval caller can act on."""

from __future__ import annotations

from harborrag_core.contracts.errors import HarborCapabilityError, HarborNoIndexedContentError


def no_indexed_content() -> HarborNoIndexedContentError:
    """Report a missing collection as "nothing has been ingested yet".

    Ingestion is what creates the collection, so its absence means this tenant
    has never ingested -- a state the caller resolves by ingesting rather than
    a fault to retry. Translated because the vector adapter raises a bare
    ``RuntimeError`` outside the Harbor hierarchy, which every caller above
    turns into "the service is unavailable". The adapter's own message names
    the physical collection, so it is logged and never forwarded.
    """

    return HarborNoIndexedContentError(
        "No content has been ingested yet, so there is nothing to search"
    )


def no_entity_index() -> HarborCapabilityError:
    """Report entity search as a capability this deployment has not turned on.

    It needs the summary projection publishing entity points
    (``HARBORRAG_SUMMARY_ENTITY_INDEX_ENABLED``) -- a configuration choice, not a
    fault -- so the caller learns what to enable rather than that something broke.
    """

    return HarborCapabilityError(
        "entity search is not enabled; set HARBORRAG_SUMMARY_ENTITY_INDEX_ENABLED"
    )


__all__ = ["no_entity_index", "no_indexed_content"]
