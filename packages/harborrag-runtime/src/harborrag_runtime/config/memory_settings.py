"""Conversation-memory settings, mixed into RuntimeSettings.

Kept in its own module so the memory policy knobs stay readable as a group
and ``settings.py`` does not grow past the file-length gate. Every field
carries the ``HARBORRAG_`` prefix its host settings class declares.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from harborrag_core.ports.memory import MemoryScope

_RECALLABLE_SCOPES = frozenset(
    {
        MemoryScope.SESSION.value,
        MemoryScope.USER.value,
        MemoryScope.PROJECT.value,
        MemoryScope.TENANT.value,
    }
)
_DEFAULT_RECALL_SCOPES = "user,project,session,tenant"


class MemorySettingsMixin(BaseSettings):
    """Policy for what conversation history and long-term memory reach a prompt.

    ``memory_enabled`` off restores the pre-memory-layer behaviour: a short
    verbatim window and no recall, summarization, rewriting, or extraction.
    """

    memory_enabled: bool = True

    # Short-term window.
    memory_recent_max_messages: int = Field(default=12, ge=1, le=200)
    memory_recent_max_tokens: int = Field(default=2_000, ge=100, le=200_000)

    # Rolling session summary. ``keep_messages`` is at least one because
    # ``MemoryPolicy`` rejects a zero verbatim tail; every bound here mirrors
    # the policy's own validation so settings that validate always map onto
    # a policy instead of raising mid-turn.
    memory_summary_trigger_fraction: float = Field(default=0.7, ge=0.1, le=1.0)
    memory_summary_keep_messages: int = Field(default=8, ge=1, le=200)

    # Long-term recall.
    memory_recall_top_k: int = Field(default=6, ge=0, le=50)
    memory_recall_scopes: str = _DEFAULT_RECALL_SCOPES
    memory_recall_recency_half_life_hours: float = Field(default=168.0, gt=0.0)
    memory_block_budget_fraction: float = Field(default=0.15, gt=0.0, le=0.5)
    # How strongly recall boosts a memory whose type the question asked for.
    # The wanted types come from the same model call that rewrites the query,
    # so the hint costs no extra request, and it only ever re-weights: every
    # recallable type is still searched. 0 disables the type term, leaving
    # ranking on relevance, recency, importance, and entity overlap alone.
    memory_type_affinity_weight: float = Field(default=0.5, ge=0.0, le=2.0)

    # History-aware retrieval.
    memory_query_rewrite: bool = True

    # Knowledge-graph entity linking. On, extraction resolves the surface forms
    # the model proposed onto curated graph node ids and recall boosts the
    # memories whose ids overlap what this turn's document retrieval surfaced.
    # Memory is only ever *linked* to the graph by id; nothing is written into
    # it. Off restores the pre-anchoring behaviour: mentions stay unresolved
    # and recall ranks on relevance, recency, and importance alone.
    memory_entity_linking: bool = True

    # Agent memory tools. Off by default because they let an agent run read and
    # record long-term memory for the caller it is acting as; the owner fields
    # are always bound server-side, never supplied by the model.
    memory_agent_tools: bool = False

    # Background extraction.
    memory_extraction_enabled: bool = True
    memory_extraction_min_importance: float = Field(default=0.3, ge=0.0, le=1.0)
    memory_dedup_threshold: float = Field(default=0.92, ge=0.5, le=1.0)

    # Model selection for rewrite, extraction, and summarization.
    memory_model_profile: str = Field(default="memory", min_length=1)
    memory_embed_profile: str | None = None

    # Retention per scope; 0 means no automatic expiry.
    memory_retention_days_session: int = Field(default=90, ge=0)
    memory_retention_days_user: int = Field(default=365, ge=0)
    memory_retention_days_project: int = Field(default=0, ge=0)

    memory_pii_redaction: bool = False

    @field_validator("memory_recall_scopes")
    @classmethod
    def validate_recall_scopes(cls, value: str) -> str:
        """Accept a comma-separated list of recallable scope names, in priority order."""

        names = [part.strip().lower() for part in value.split(",")]
        present = [name for name in names if name]
        if not present:
            raise ValueError(
                "HARBORRAG_MEMORY_RECALL_SCOPES must name at least one scope; "
                f"recallable scopes are {sorted(_RECALLABLE_SCOPES)}"
            )
        unknown = sorted(set(present) - _RECALLABLE_SCOPES)
        if unknown:
            raise ValueError(
                f"HARBORRAG_MEMORY_RECALL_SCOPES contains unrecallable scopes {unknown}; "
                f"recallable scopes are {sorted(_RECALLABLE_SCOPES)}"
            )
        if len(set(present)) != len(present):
            raise ValueError("HARBORRAG_MEMORY_RECALL_SCOPES must not repeat a scope")
        return ",".join(present)

    @model_validator(mode="after")
    def validate_memory_window(self) -> MemorySettingsMixin:
        """Keep the verbatim tail inside the window it is a tail of."""

        if self.memory_summary_keep_messages > self.memory_recent_max_messages:
            raise ValueError(
                "HARBORRAG_MEMORY_SUMMARY_KEEP_MESSAGES "
                f"({self.memory_summary_keep_messages}) must not exceed "
                f"HARBORRAG_MEMORY_RECENT_MAX_MESSAGES ({self.memory_recent_max_messages})"
            )
        return self

    @property
    def memory_recall_scope_order(self) -> tuple[MemoryScope, ...]:
        """Recall scopes as enum members, in the configured priority order."""

        return tuple(MemoryScope(name) for name in self.memory_recall_scopes.split(","))

    def memory_retention_days(self, scope: MemoryScope) -> int:
        """Retention for one scope in days; 0 means the scope never auto-expires."""

        if scope is MemoryScope.SESSION or scope is MemoryScope.RUN:
            return self.memory_retention_days_session
        if scope is MemoryScope.USER:
            return self.memory_retention_days_user
        if scope is MemoryScope.PROJECT:
            return self.memory_retention_days_project
        return 0


__all__ = ["MemorySettingsMixin"]
