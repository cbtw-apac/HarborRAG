"""Provider-independent durable indexing admission and usage accounting."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from harborrag_core.base import StrictModel


class IndexingBudgetLimits(StrictModel):
    """Durable tenant limits for graph-building LLM operations only."""

    max_concurrency: int = Field(default=4, ge=1, le=100)
    max_job_attempts: int = Field(default=10, ge=1, le=10)
    daily_token_cap: int = Field(default=2000000, ge=0)
    daily_cost_usd: Decimal = Field(default=Decimal("10"), ge=0)
    reservation_seconds: int = Field(default=300, ge=1, le=3600)


class BudgetRequest(StrictModel):
    reservation_id: str = Field(min_length=1, max_length=128)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    purpose: Literal["extraction", "parent_description", "consolidation"] = (
        "extraction"
    )
    operation_key: str | None = Field(default=None, max_length=128)
    provider_calls: int = Field(default=1, ge=1, le=8)
    max_provider_calls: int = Field(default=8, ge=1, le=8)


class BudgetReservation(StrictModel):
    reservation_id: str
    tenant_id: str
    job_id: str
    fence: int
    reserved_tokens: int
    reserved_cost_usd: Decimal
    expires_at: datetime
    state: Literal["reserved", "settled"] = "reserved"


class UsageSettlement(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)


class BudgetAdmission(StrictModel):
    admitted: bool
    reservation: BudgetReservation | None = None
    reason: (
        Literal[
            "spending_paused",
            "concurrency",
            "daily_tokens",
            "daily_cost",
            "reservation_exists",
            "operation_call_cap",
        ]
        | None
    ) = None
    retry_after: datetime | None = None
