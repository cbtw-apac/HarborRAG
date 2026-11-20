"""Safe page input selected from Confluence API payloads for the normalizer.

Only the fields declared on ConfluencePageInput leave the API payload, so
credentials and other provider fields never reach a Document.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .values import mapping as _mapping
from .values import mapping_sequence as _mapping_sequence


def space_identity(
    space: Mapping[str, object],
    *,
    default_space_key: str = "",
) -> dict[str, str]:
    """Resolve a space's two identity fields, in exactly one place.

    The graph keys a space node by ``space_id``, so a second copy of this fallback
    would split one space into two nodes as soon as the copies drift. Pages and
    their attachments both take their space identity from here.

    Args:
        space: The ``space`` object of a content API payload.
        default_space_key: Space key to use when the payload names none.

    Returns:
        ``space_id`` (the provider ID, else the key) and ``space_key``.
    """
    return {
        "space_id": str(space.get("id") or space.get("key") or default_space_key),
        "space_key": str(space.get("key") or default_space_key),
    }


@dataclass(frozen=True, slots=True)
class ConfluencePageInput:
    """Safe page fields accepted by the canonical normalizer.

    Attributes:
        page_id: Confluence content ID of the page.
        page_version: Version number, else the version timestamp.
        space_id: Provider ID of the page's space, else its key.
        space_key: Key of the page's space.
        title: Page title.
        source_url: User-facing URL of the page.
        document_id: Canonical document ID, derived from the page when None.
        document_version_id: Canonical version ID, derived from the page when None.
        ancestors: ``(id, title)`` pairs of the page's ancestors, in API order.
        labels: Label names on the page.
        adf: Atlassian Document Format body, when the API returned one.
        storage: Storage-format body, when not blank.
        rendered_html: Export view body, else the view body, when not blank.
        permissions: Permission data copied to provenance, read-only.
    """

    page_id: str
    page_version: str
    space_id: str
    space_key: str
    title: str
    source_url: str
    document_id: str | None = None
    document_version_id: str | None = None
    ancestors: tuple[tuple[str, str], ...] = ()
    labels: tuple[str, ...] = ()
    adf: Mapping[str, Any] | str | None = None
    storage: str | None = None
    rendered_html: str | None = None
    permissions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject blank identity, title, ancestor and label values.

        Raises:
            ValueError: A required value is empty or only whitespace.
        """
        required = (
            self.page_id,
            self.page_version,
            self.space_id,
            self.space_key,
            self.title,
            self.source_url,
        )
        if any(not value.strip() for value in required):
            raise ValueError("Confluence page identity and title values must be non-empty")
        if self.document_id is not None and not self.document_id.strip():
            raise ValueError("document_id must be non-empty when provided")
        if self.document_version_id is not None and not self.document_version_id.strip():
            raise ValueError("document_version_id must be non-empty when provided")
        if any(not identifier.strip() or not title.strip() for identifier, title in self.ancestors):
            raise ValueError("Confluence ancestors require non-empty IDs and titles")
        if any(not label.strip() for label in self.labels):
            raise ValueError("Confluence labels must be non-empty")
        object.__setattr__(self, "permissions", MappingProxyType(dict(self.permissions)))

    @classmethod
    def from_api_payload(
        cls,
        payload: Mapping[str, object],
        *,
        source_url: str,
        default_space_key: str = "",
    ) -> ConfluencePageInput:
        """Select safe page and body fields without retaining the API payload.

        A blank page title, such as one made only of no-break spaces, falls back
        to the page ID. An ancestor without an ID or with a blank title is left
        out. Neither fails the page. Titles that are not blank are kept as sent.

        Args:
            payload: Content API payload for one page.
            source_url: User-facing URL of the page.
            default_space_key: Space key to use when the payload names none.

        Returns:
            The page fields the normalizer reads.
        """
        body = _mapping(payload.get("body"))
        space = _mapping(payload.get("space"))
        version = _mapping(payload.get("version"))
        metadata = _mapping(payload.get("metadata"))
        labels = _mapping(metadata.get("labels")).get("results")
        identity = space_identity(space, default_space_key=default_space_key)
        page_id = str(payload.get("id") or "")
        return cls(
            page_id=page_id,
            page_version=str(version.get("number") or version.get("when") or ""),
            space_id=identity["space_id"],
            space_key=identity["space_key"],
            title=_page_title(payload, page_id),
            source_url=source_url,
            ancestors=_titled_ancestors(payload),
            labels=tuple(
                str(item.get("name")) for item in _mapping_sequence(labels) if item.get("name")
            ),
            adf=_body_value(body, "atlas_doc_format"),
            storage=_text_body_value(body, "storage"),
            rendered_html=(_text_body_value(body, "export_view") or _text_body_value(body, "view")),
        )


def _page_title(payload: Mapping[str, object], page_id: str) -> str:
    """Return the page title as sent, or the page ID when the title is blank."""
    title = str(payload.get("title") or "")
    return title if title.strip() else page_id


def _titled_ancestors(payload: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    """Return ``(id, title)`` pairs of the ancestors whose ID and title are not blank."""
    pairs = (
        (str(item.get("id") or ""), str(item.get("title") or ""))
        for item in _mapping_sequence(payload.get("ancestors"))
    )
    return tuple(
        (identifier, title) for identifier, title in pairs if identifier.strip() and title.strip()
    )


def _body_value(body: Mapping[str, object], key: str) -> Mapping[str, Any] | str | None:
    """Return one body representation's value when it is a mapping or a string."""
    value = _mapping(body.get(key)).get("value")
    return value if isinstance(value, (Mapping, str)) else None


def _text_body_value(body: Mapping[str, object], key: str) -> str | None:
    """Return one body representation's value when it is a non-blank string."""
    value = _body_value(body, key)
    return value if isinstance(value, str) and value.strip() else None
