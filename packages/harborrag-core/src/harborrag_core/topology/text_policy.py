"""Human-readable text limits for generated graph views.

The graph stores navigation text, not source passages.  Source text and complete
generation lineage remain in Qdrant and the canonical topology artifacts.
"""

from __future__ import annotations

import re

ENTITY_NAME_MAX_CHARS = 80
ENTITY_NAME_MAX_WORDS = 12
ENTITY_DESCRIPTION_MAX_CHARS = 280
ENTITY_DESCRIPTION_MAX_WORDS = 35
CHUNK_TITLE_MAX_CHARS = 120
CHUNK_TITLE_MAX_WORDS = 14
CHUNK_DESCRIPTION_MAX_CHARS = 400
CHUNK_DESCRIPTION_MAX_WORDS = 50
EMBEDDING_CONTEXT_MAX_CHARS = 400
EMBEDDING_CONTEXT_MAX_WORDS = 50
RELATION_DESCRIPTION_MAX_CHARS = 400
RELATION_DESCRIPTION_MAX_WORDS = 50
PARENT_DESCRIPTION_MAX_CHARS = 480
PARENT_DESCRIPTION_MAX_WORDS = 60

_WORD = re.compile(r"\S+")


def enforce_text_budget(value: str, *, field: str, max_words: int) -> str:
    """Return stripped text or reject prose that exceeds its semantic budget."""

    stripped = value.strip()
    if len(_WORD.findall(stripped)) > max_words:
        raise ValueError(f"{field} exceeds the {max_words}-word graph text budget")
    return stripped


def enforce_entity_name(value: str) -> str:
    """Reject values and sentences masquerading as graph entities."""

    stripped = enforce_text_budget(
        value,
        field="entity name",
        max_words=ENTITY_NAME_MAX_WORDS,
    )
    if not any(character.isalpha() for character in stripped):
        raise ValueError("entity name must be a named concept, not a numeric value")
    if stripped.endswith((".", "!", "?", ";")):
        raise ValueError("entity name must be a noun phrase, not a sentence")
    return stripped
