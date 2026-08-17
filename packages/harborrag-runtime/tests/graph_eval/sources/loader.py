"""Load the graph eval sample corpus from its per-source JSON fixture tree.

One directory per source type under ``fixtures/``. Every ``*.json`` beside that
directory's ``_defaults.json`` is one sample document, so adding a sample is adding a
file and adding a source type is adding a directory -- neither touches Python.

Envelope; file keys win over the directory's ``_defaults.json``::

    {
      "id": "team-handbook",
      "title": "Team Handbook",
      "content_type": "page",
      "extra": {"page_id": "team-handbook"},
      "note": "3-level heading tree with a nested comment reply chain",
      "elements": [
        {"id": "h1", "type": "heading", "content": "Team Handbook",
         "metadata": {"level": 1}}
      ],
      "relations": [
        {"predicate": "child_of", "target_id": "space-overview",
         "target_type": "document"}
      ]
    }

``note`` says what projection shape the sample is here to exercise. It is required and
never reaches the ``Document`` -- a sample nobody can explain is a sample nobody can
maintain.

Deliberately not the canonical-codec envelope (``load_canonical_document``): fixtures
stay hand-authorable, and this loader validates element types against the domain
vocabulary with per-file errors, which the codec does not.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast, get_args

from harborrag_core.domain.document import Document, DocumentRelation
from harborrag_core.domain.element import DocumentElement, ElementType
from harborrag_core.domain.provenance import DocumentProvenance

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEFAULTS_NAME = "_defaults.json"

# Read off the domain Literal rather than restated here, so a new element type is
# accepted the moment the domain accepts it.
ELEMENT_TYPES: frozenset[str] = frozenset(get_args(ElementType))
_REQUIRED_KEYS = ("id", "title", "note", "elements")


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"{path.name}: missing ({error})") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{path.name}: invalid JSON ({error})") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: fixture must be a JSON object")
    return payload


def _element(raw: Any, origin: str) -> DocumentElement:
    if not isinstance(raw, dict):
        raise ValueError(f"{origin}: every element must be a JSON object")
    if "id" not in raw:
        raise ValueError(f"{origin}: element missing 'id'")
    element_type = raw.get("type")
    if element_type not in ELEMENT_TYPES:
        raise ValueError(f"{origin}: unknown element type {element_type!r}")
    return DocumentElement(
        id=str(raw["id"]),
        type=cast(ElementType, element_type),
        content=raw.get("content"),
        metadata=dict(raw.get("metadata", {})),
    )


def _relation(raw: Any, origin: str) -> DocumentRelation:
    if not isinstance(raw, dict):
        raise ValueError(f"{origin}: every relation must be a JSON object")
    missing = [key for key in ("predicate", "target_id") if key not in raw]
    if missing:
        raise ValueError(f"{origin}: relation missing {missing}")
    return DocumentRelation(
        predicate=str(raw["predicate"]),
        target_id=str(raw["target_id"]),
        target_type=str(raw.get("target_type", "document")),
    )


def load_document(path: Path, defaults: Mapping[str, Any]) -> Document:
    """Build one ``Document`` from a sample file layered over its directory defaults."""

    payload = _read_object(path)
    missing = [key for key in _REQUIRED_KEYS if key not in payload]
    if missing:
        raise ValueError(f"{path.name}: missing required keys {missing}")
    source = payload.get("source", defaults.get("source"))
    content_type = payload.get("content_type", defaults.get("content_type"))
    if not source or not content_type:
        raise ValueError(
            f"{path.name}: 'source' and 'content_type' must be set here or in {DEFAULTS_NAME}"
        )
    elements = payload["elements"]
    if not isinstance(elements, list) or not elements:
        raise ValueError(f"{path.name}: 'elements' must be a non-empty list")
    relations = payload.get("relations", [])
    if not isinstance(relations, list):
        raise ValueError(f"{path.name}: 'relations' must be a list")
    return Document(
        id=str(payload["id"]),
        title=str(payload["title"]),
        content=[_element(raw, path.name) for raw in elements],
        content_type=str(content_type),
        provenance=DocumentProvenance(
            source=str(source),
            record_id=str(payload["id"]),
            extra={**defaults.get("extra", {}), **payload.get("extra", {})},
        ),
        relations=[_relation(raw, path.name) for raw in relations],
        raw=None,
    )


def eval_documents() -> dict[str, Document]:
    """Every sample document, keyed by document id, freshly built on each call."""

    documents: dict[str, Document] = {}
    for directory in sorted(path for path in FIXTURES.iterdir() if path.is_dir()):
        defaults = _read_object(directory / DEFAULTS_NAME)
        for path in sorted(directory.glob("*.json")):
            if path.name == DEFAULTS_NAME:
                continue
            document = load_document(path, defaults)
            if document.id in documents:
                raise ValueError(f"{path.name}: duplicate document id {document.id!r}")
            documents[document.id] = document
    if not documents:
        raise ValueError(f"no sample documents found under {FIXTURES}")
    return documents
