"""Versioned engineering limits, not benchmark-derived quality guarantees."""

from __future__ import annotations

from pydantic import Field

from harborrag_core.base import StrictModel


class TopologyRetrievalPolicy(StrictModel):
    version: str = Field(default="bounded-evidence-v2", min_length=1)
    rrf_constant: int = Field(default=60, ge=1, le=1000)
    semantic_weight: float = Field(default=0.5, ge=0, le=1)
    seed_chunks: int = Field(default=8, ge=1, le=8)
    max_entities: int = Field(default=50, ge=1, le=50)
    max_candidate_chunks: int = Field(default=100, ge=1, le=100)
    typed_relation_hops: int = Field(default=2, ge=0, le=2)
    protected_direct_passages: int = Field(default=4, ge=0, le=100)
    target_passages: int = Field(default=10, ge=1, le=100)
    max_context_tokens: int = Field(default=8000, ge=1, le=100000)
    # Authorized mention degree only. Saturated reads cannot establish a safe
    # degree; skip that entity's incidence route rather than truncate arbitrarily.
    max_entity_mentions: int = Field(default=100, ge=1, le=100)
