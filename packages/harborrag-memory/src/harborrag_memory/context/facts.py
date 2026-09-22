"""The extraction schema the model fills in, and its validated in-process form.

``ProposedFacts`` is what ``with_structured_output`` binds: every field is
permissive and defaulted so one malformed entry cannot invalidate the whole
response. ``ExtractedFact`` is what the extractor stores: a value object whose
type, scope, and importance have already been coerced into range.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from math import isfinite
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from harborrag_core.ports.memory import MemoryScope, MemoryType

from .strict_schema import STRICT_SCHEMA_CONFIG

EXTRACTION_TYPES: dict[str, MemoryType] = {
    MemoryType.FACT.value: MemoryType.FACT,
    MemoryType.PREFERENCE.value: MemoryType.PREFERENCE,
    MemoryType.DECISION.value: MemoryType.DECISION,
    MemoryType.EPISODE.value: MemoryType.EPISODE,
}
"""Types a conversation may produce. Summaries and working state are not extracted."""

EXTRACTION_SCOPES: dict[str, MemoryScope] = {
    MemoryScope.SESSION.value: MemoryScope.SESSION,
    MemoryScope.USER.value: MemoryScope.USER,
    MemoryScope.PROJECT.value: MemoryScope.PROJECT,
}
"""Scopes a conversation may write. TENANT and GLOBAL are never inferred from chat."""

_RE_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)

DEFAULT_TYPE = MemoryType.FACT
DEFAULT_SCOPE = MemoryScope.SESSION
"""An unparseable scope falls back to the narrowest one, never the widest."""


@dataclass(frozen=True, slots=True)
class ExtractedFact:
    """One atomic fact the model proposed storing, already coerced into range.

    ``replaces`` carries the bracketed reference of the existing memory this
    fact updates, or ``None``. It is a reference into the memories rendered
    into the prompt, so the extractor resolves it by exact lookup and ignores
    anything it does not recognise.
    """

    content: str
    memory_type: MemoryType
    scope: MemoryScope
    importance: float
    entities: tuple[str, ...] = ()
    replaces: str | None = None


class ProposedFact(BaseModel):
    """One entry of the model's structured extraction response."""

    # Bound by ``with_structured_output``: see ``strict_schema`` for what
    # OpenAI rejects without this, and why it is declared on the schema
    # instead of enforced at parse time.
    model_config = STRICT_SCHEMA_CONFIG

    content: str = Field(default="", description="One atomic third-person statement.")
    memory_type: str = Field(
        default=DEFAULT_TYPE.value,
        description="One of: fact, preference, decision, episode.",
    )
    scope: str = Field(
        default=DEFAULT_SCOPE.value,
        description="One of: session, user, project.",
    )
    importance: float = Field(default=0.5, description="How durable this fact is, 0 to 1.")
    entities: list[str] = Field(
        default_factory=list,
        description="People, systems, or projects this fact is about.",
    )
    replaces: str = Field(
        default="",
        description="Bracketed reference of the existing memory this fact replaces, else empty.",
    )


class ProposedFacts(BaseModel):
    """The model's whole extraction response."""

    # Bound by ``with_structured_output``: see ``strict_schema`` for what
    # OpenAI rejects without this, and why it is declared on the schema
    # instead of enforced at parse time.
    model_config = STRICT_SCHEMA_CONFIG

    facts: list[ProposedFact] = Field(default_factory=list)


def clamp_importance(value: float) -> float:
    """Return ``value`` confined to the 0..1 range ``Memory`` accepts."""

    if not isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


def to_fact(proposed: ProposedFact) -> ExtractedFact | None:
    """Coerce one proposal into a storable fact, or ``None`` when it is blank."""

    content = " ".join(proposed.content.split())
    if not content:
        return None
    entities = tuple(dict.fromkeys(name.strip() for name in proposed.entities if name.strip()))
    return ExtractedFact(
        content=content,
        memory_type=EXTRACTION_TYPES.get(proposed.memory_type.strip().lower(), DEFAULT_TYPE),
        scope=EXTRACTION_SCOPES.get(proposed.scope.strip().lower(), DEFAULT_SCOPE),
        importance=clamp_importance(proposed.importance),
        entities=entities,
        replaces=proposed.replaces.strip() or None,
    )


def as_proposals(payload: Any) -> ProposedFacts:
    """Read a structured-output payload however the provider shaped it."""

    if isinstance(payload, ProposedFacts):
        return payload
    if isinstance(payload, BaseModel):
        return ProposedFacts.model_validate(payload.model_dump())
    if isinstance(payload, list):
        return ProposedFacts.model_validate({"facts": payload})
    if isinstance(payload, dict):
        return ProposedFacts.model_validate(payload)
    raise TypeError(f"unsupported extraction payload: {type(payload).__name__}")


def facts_from_text(text: str) -> ProposedFacts | None:
    """Read a JSON extraction response out of a plain-text completion.

    ``None`` -- never an exception -- when there is no JSON object in the
    text or it does not describe facts. The fallback lane exists because a
    provider refused the schema, so it has to tolerate the looser output
    that asking in prose produces: a ``` fence around the object, or a
    sentence before it.
    """

    if not text or not text.strip():
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = _RE_FENCE.sub("", candidate).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(candidate[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return as_proposals(payload)
    except (TypeError, ValidationError):
        return None


def extracted_facts(payload: Any, *, min_importance: float) -> tuple[ExtractedFact, ...]:
    """Return the blank-free facts from ``payload`` that clear ``min_importance``."""

    proposals = as_proposals(payload)
    facts = (to_fact(proposed) for proposed in proposals.facts)
    return tuple(fact for fact in facts if fact is not None and fact.importance >= min_importance)


__all__ = [
    "DEFAULT_SCOPE",
    "DEFAULT_TYPE",
    "EXTRACTION_SCOPES",
    "EXTRACTION_TYPES",
    "ExtractedFact",
    "ProposedFact",
    "ProposedFacts",
    "as_proposals",
    "clamp_importance",
    "extracted_facts",
    "facts_from_text",
    "to_fact",
]
