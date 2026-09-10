"""Durable model-usage accounting port.

One ``ModelUsageRecord`` is the billing- and quota-relevant footprint of a
single model call: who it was for (``tenant_id``/``user_id``, with
``principal_id`` kept as the credential that acted), where it came from
(``surface``, ``session_id``, ``run_id``), which model actually served it
(``logical_model`` as requested versus ``provider``/``provider_model`` as
resolved), and what it cost in tokens and money.

Recording is deliberately best-effort at the adapter boundary: accounting
must never turn a paid-for answer into an error (see
``ModelUsageRepository.record``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import uuid4

from harborrag_core.base import utc_now

UsageSurface = Literal["chat", "agent"]
"""Which completion surface issued the model call."""


def new_usage_id() -> str:
    """Generate one opaque model-usage record identifier."""

    return f"usage-{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class ModelUsageRecord:
    """One model call's token and cost footprint, attributed to a human."""

    tenant_id: str
    user_id: str
    principal_id: str
    surface: UsageSurface
    logical_model: str
    provider: str
    provider_model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    session_id: str | None = None
    run_id: str | None = None
    estimated_cost_usd: float | None = None
    finish_reason: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    id: str = field(default_factory=new_usage_id)

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.user_id.strip():
            raise ValueError("model usage requires a tenant and a user")
        if min(self.prompt_tokens, self.completion_tokens, self.total_tokens) < 0:
            raise ValueError("model usage token counts must not be negative")
        if self.estimated_cost_usd is not None and self.estimated_cost_usd < 0:
            raise ValueError("model usage estimated_cost_usd must not be negative")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("model usage created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ModelUsageTotals:
    """Aggregated usage over a tenant, optionally one user, optionally a window."""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


class ModelUsageRepository(Protocol):
    """Persistence-neutral model-usage accounting contract."""

    async def record(self, usage: ModelUsageRecord) -> None:
        """Persist one usage record; never raises into the caller.

        A failed write is logged with identifiers only and swallowed:
        accounting is not allowed to fail an answer the caller already paid
        the provider for.
        """
        ...

    async def totals(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        since: datetime | None = None,
    ) -> ModelUsageTotals:
        """Aggregate recorded usage for a tenant, one user, and/or a window."""
        ...


__all__ = [
    "ModelUsageRecord",
    "ModelUsageRepository",
    "ModelUsageTotals",
    "UsageSurface",
    "new_usage_id",
]
