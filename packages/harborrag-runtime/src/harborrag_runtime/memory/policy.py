"""Translate runtime memory settings into the memory layer's policy object.

``harborrag_memory`` owns the behaviour; ``RuntimeSettings`` owns the knobs.
This module is the single place the two vocabularies meet, so a renamed
setting fails here rather than silently feeding a default into a prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_memory import MemoryPolicy

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings


def memory_policy_from_settings(settings: RuntimeSettings) -> MemoryPolicy:
    """Build the memory policy the configured runtime asks for.

    ``memory_enabled`` off yields :meth:`MemoryPolicy.disabled`, which keeps
    the pre-memory-layer behaviour: a short verbatim window and no recall,
    summarization, or rewriting.
    """

    if not settings.memory_enabled:
        return MemoryPolicy.disabled()
    return MemoryPolicy(
        enabled=True,
        recent_max_messages=settings.memory_recent_max_messages,
        recent_max_tokens=settings.memory_recent_max_tokens,
        summary_trigger_fraction=settings.memory_summary_trigger_fraction,
        summary_keep_messages=settings.memory_summary_keep_messages,
        recall_top_k=settings.memory_recall_top_k,
        recall_scopes=settings.memory_recall_scope_order,
        recency_half_life_hours=settings.memory_recall_recency_half_life_hours,
        block_budget_fraction=settings.memory_block_budget_fraction,
        type_affinity_weight=settings.memory_type_affinity_weight,
        query_rewrite=settings.memory_query_rewrite,
    )


__all__ = ["memory_policy_from_settings"]
