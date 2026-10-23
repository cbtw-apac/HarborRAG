"""Validated page input for the Confluence normalizer.

Handles:
- Page identity, label and ancestor fields
- ADF, storage and rendered HTML body values
- Space identity resolution shared with attachment binding
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .values import mapping as _mapping
from .values import mapping_sequence as _mapping_sequence


def space_identity(
    space: Mapping[str, Any],
    *,
    default_space_key: str = "",
) -> dict[str, str]:
    """Resolve a space's ``space_id`` and ``space_key``.

    Pages and attachment bindings both use it so they share one space node.

    Args:
        space: Space mapping from the API payload.
        default_space_key: Key used when the payload has neither ``id`` nor ``key``.

    Returns:
        Mapping with ``space_id`` (id, else key, else default) and ``space_key``.
    """

    return {
        "space_id": str(space.get("id") or space.get("key") or default_space_key),
        "space_key": str(space.get("key") or default_space_key),
    }


@dataclass(frozen=True, slots=True)
class ConfluencePageInput:
    """Validated page fields accepted by the normalizer.

    Attributes:
        document_id: Overrides the derived ``confluence://`` document id.
        document_version_id: Overrides the derived document version id.
        ancestors: ``(id, title)`` pairs from the root down to the parent.
        adf: ADF body as a mapping or JSON string.
        storage: Storage-format XHTML body.
        rendered_html: Rendered export or view HTML body.
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
        payload: Mapping[str, Any],
        *,
        source_url: str,
        default_space_key: str = "",
    ) -> ConfluencePageInput:
        """Build a page input from a Confluence API payload.

        Keeps only the identity, label, ancestor and body fields; the payload itself is
        not retained.

        Args:
            payload: Page payload from the Confluence API.
            source_url: Canonical URL of the page.
            default_space_key: Space key used when the payload carries none.

        Returns:
            Validated page input.

        Raises:
            ValueError: If a required field resolves to a blank value.
        """

        body = _mapping(payload.get("body"))
        space = _mapping(payload.get("space"))
        version = _mapping(payload.get("version"))
        metadata = _mapping(payload.get("metadata"))
        labels = _mapping(metadata.get("labels")).get("results")
        ancestors = tuple(
            (str(item.get("id") or ""), str(item.get("title") or ""))
            for item in _mapping_sequence(payload.get("ancestors"))
            if item.get("id") and item.get("title")
        )
        identity = space_identity(space, default_space_key=default_space_key)
        return cls(
            page_id=str(payload.get("id") or ""),
            page_version=str(version.get("number") or version.get("when") or ""),
            space_id=identity["space_id"],
            space_key=identity["space_key"],
            title=str(payload.get("title") or ""),
            source_url=source_url,
            ancestors=ancestors,
            labels=tuple(
                str(item.get("name")) for item in _mapping_sequence(labels) if item.get("name")
            ),
            adf=_body_value(body, "atlas_doc_format"),
            storage=_text_body_value(body, "storage"),
            rendered_html=(_text_body_value(body, "export_view") or _text_body_value(body, "view")),
        )


def _body_value(body: Mapping[str, Any], key: str) -> Mapping[str, Any] | str | None:
    """Return a body representation's value if it is a mapping or string."""

    value = _mapping(body.get(key)).get("value")
    return value if isinstance(value, (Mapping, str)) else None


def _text_body_value(body: Mapping[str, Any], key: str) -> str | None:
    """Return a body representation's value if it is a non-blank string."""

    value = _body_value(body, key)
    return value if isinstance(value, str) and value.strip() else None
