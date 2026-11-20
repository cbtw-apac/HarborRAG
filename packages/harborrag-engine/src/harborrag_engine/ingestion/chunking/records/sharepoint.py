"""SharePoint document fields copied onto every chunk of a document version.

Admission fingerprints exactly what ``sharepoint_chunk_fields`` returns, so a
change to any field a chunk carries from here makes a new document version.

Typical usage example:

    metadata = with_sharepoint_fields(document, candidate.metadata)
    fingerprint_input = sharepoint_chunk_fields(document)
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from harborrag_core.domain.document import Document

_EXTRA_IDENTITY_KEYS = ("site_id", "drive_id", "item_id", "page_id")
_DOCUMENT_OWNED_KEYS = frozenset((*_EXTRA_IDENTITY_KEYS, "web_url"))


def sharepoint_chunk_fields(document: Document) -> dict[str, str]:
    """Return every field ``with_sharepoint_fields`` copies from the document.

    Args:
        document: A normalized SharePoint document.

    Returns:
        The site, drive, item and page IDs, the web URL, the author, and the
        creation and update times in UTC. Missing and blank values are left out.
    """
    return {**_timestamps(document), **_identity(document)}


def with_sharepoint_fields(
    document: Document,
    candidate_metadata: Mapping[str, object],
) -> dict[str, object]:
    """Merge a SharePoint document's fields into a chunk candidate's metadata.

    The document's times fill in only where the candidate has none. Its site,
    drive, item and page IDs and its web URL replace any candidate copy, even
    when the document has no value for one of them, so a chunk is never
    attributed to another drive item. The document's author, when it has one,
    replaces the candidate's.

    Args:
        document: The SharePoint document the candidate was cut from.
        candidate_metadata: Metadata the chunking strategy produced.

    Returns:
        A new mapping. ``candidate_metadata`` is left unchanged.
    """
    candidate = {
        key: value for key, value in candidate_metadata.items() if key not in _DOCUMENT_OWNED_KEYS
    }
    return {**_timestamps(document), **candidate, **_identity(document)}


def _identity(document: Document) -> dict[str, str]:
    """Return the document's IDs, web URL and author, leaving out blank values."""
    provenance = document.provenance
    values: dict[str, object] = {key: provenance.extra.get(key) for key in _EXTRA_IDENTITY_KEYS}
    values["web_url"] = provenance.url
    values["author"] = provenance.author
    return {
        key: text for key, value in values.items() if (text := _stripped_text(value)) is not None
    }


def _timestamps(document: Document) -> dict[str, str]:
    """Return the document's creation and update times in UTC, when known."""
    provenance = document.provenance
    values = {
        "source_created_at": _utc_isoformat(provenance.created_at),
        "source_updated_at": _utc_isoformat(provenance.updated_at),
    }
    return {key: value for key, value in values.items() if value is not None}


def _stripped_text(value: object) -> str | None:
    """Return a string or integer as stripped text.

    Any other type, and blank text, gives ``None``.
    """
    if not isinstance(value, (str, int)):
        return None
    return str(value).strip() or None


def _utc_isoformat(value: datetime | None) -> str | None:
    """Return a time as ISO 8601 in UTC, reading a naive time as UTC.

    A time with no UTC equivalent, such as ``0001-01-01T00:00:00+01:00``, is
    treated as unknown instead of failing the whole document.
    """
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        return aware.astimezone(UTC).isoformat()
    except OverflowError:
        return None
