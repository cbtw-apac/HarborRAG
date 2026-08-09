from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .values import mapping as _mapping
from .values import mapping_sequence as _mapping_sequence


@dataclass(frozen=True, slots=True)
class ConfluencePageInput:
    """Safe page fields accepted by the canonical normalizer."""

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
        """Select safe page and body fields without retaining the API payload."""

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
        return cls(
            page_id=str(payload.get("id") or ""),
            page_version=str(version.get("number") or version.get("when") or ""),
            space_id=str(space.get("id") or space.get("key") or default_space_key),
            space_key=str(space.get("key") or default_space_key),
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
    value = _mapping(body.get(key)).get("value")
    return value if isinstance(value, (Mapping, str)) else None


def _text_body_value(body: Mapping[str, Any], key: str) -> str | None:
    value = _body_value(body, key)
    return value if isinstance(value, str) and value.strip() else None
