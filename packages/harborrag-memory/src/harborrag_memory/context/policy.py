"""Per-turn conversation-memory policy.

One immutable knob set decides how much history a turn replays verbatim, when
the rolling session summary is refreshed, and whether the question is
condensed into a standalone retrieval query. Every value is validated at
construction so an invalid policy fails at wiring time, not mid-turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from harborrag_core.ports.memory import MemoryScope

from ..errors import MemoryConfigurationError

DEFAULT_RECALL_SCOPES: tuple[MemoryScope, ...] = (
    MemoryScope.USER,
    MemoryScope.PROJECT,
    MemoryScope.SESSION,
    MemoryScope.TENANT,
)
"""Scopes long-term recall consults, narrowest-to-broadest but session-safe."""


def _require_positive(name: str, value: float) -> None:
    if not isfinite(value) or value <= 0:
        raise MemoryConfigurationError(f"memory policy {name} must be a positive number")


def _require_fraction(name: str, value: float) -> None:
    if not isfinite(value) or not 0.0 < value <= 1.0:
        raise MemoryConfigurationError(f"memory policy {name} must be between zero and one")


def _require_between(name: str, value: float, *, low: float, high: float) -> None:
    if not isfinite(value) or not low <= value <= high:
        raise MemoryConfigurationError(f"memory policy {name} must be between {low} and {high}")


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    """How one turn assembles its conversation context.

    ``extraction_min_importance`` is the floor a proposed long-term fact must
    clear to be stored, and ``dedup_threshold`` the vector similarity at or
    above which a proposed fact is treated as a restatement of one already
    stored in the same scope.

    ``entity_confidence_floor`` is the confidence a resolved entity mention
    must clear to be stored on ``Memory.entity_ids``, and
    ``entity_overlap_weight`` how strongly recall boosts a memory whose
    entities overlap the ones this turn's document retrieval surfaced -- 0
    disables entity anchoring without changing anything else.

    ``type_affinity_weight`` is how strongly recall boosts a memory whose type
    the per-turn query rewrite reported the question wants -- 0 disables the
    type hint. It only ever re-ranks: recall still searches every recallable
    type, and retention stays keyed on scope.
    """

    enabled: bool = True
    recent_max_messages: int = 12
    recent_max_tokens: int = 2000
    summary_trigger_fraction: float = 0.7
    summary_keep_messages: int = 8
    recall_top_k: int = 6
    recall_scopes: tuple[MemoryScope, ...] = DEFAULT_RECALL_SCOPES
    recency_half_life_hours: float = 168.0
    block_budget_fraction: float = 0.15
    query_rewrite: bool = True
    extraction_min_importance: float = 0.3
    dedup_threshold: float = 0.92
    entity_confidence_floor: float = 0.5
    entity_overlap_weight: float = 0.5
    type_affinity_weight: float = 0.5

    def __post_init__(self) -> None:
        _require_positive("recent_max_messages", self.recent_max_messages)
        _require_positive("recent_max_tokens", self.recent_max_tokens)
        _require_positive("summary_keep_messages", self.summary_keep_messages)
        _require_positive("recency_half_life_hours", self.recency_half_life_hours)
        _require_fraction("summary_trigger_fraction", self.summary_trigger_fraction)
        _require_fraction("block_budget_fraction", self.block_budget_fraction)
        _require_between(
            "extraction_min_importance", self.extraction_min_importance, low=0.0, high=1.0
        )
        _require_between("dedup_threshold", self.dedup_threshold, low=0.5, high=1.0)
        _require_between("entity_confidence_floor", self.entity_confidence_floor, low=0.0, high=1.0)
        _require_between("entity_overlap_weight", self.entity_overlap_weight, low=0.0, high=2.0)
        _require_between("type_affinity_weight", self.type_affinity_weight, low=0.0, high=2.0)
        if self.recall_top_k < 0:
            raise MemoryConfigurationError("memory policy recall_top_k must not be negative")
        if self.summary_keep_messages > self.recent_max_messages:
            raise MemoryConfigurationError(
                "memory policy summary_keep_messages must not exceed recent_max_messages"
            )
        if len(set(self.recall_scopes)) != len(self.recall_scopes):
            raise MemoryConfigurationError("memory policy recall_scopes must be unique")

    @classmethod
    def disabled(cls) -> MemoryPolicy:
        """Return the policy that replays a tiny window and calls no model.

        ``summary_keep_messages`` shrinks with ``recent_max_messages`` so the
        keep-at-most-the-window invariant holds unconditionally.
        """

        return cls(
            enabled=False,
            recent_max_messages=4,
            summary_keep_messages=4,
            recall_top_k=0,
            query_rewrite=False,
        )


__all__ = ["DEFAULT_RECALL_SCOPES", "MemoryPolicy"]
