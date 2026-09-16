"""Resolved tenant switch is authoritative over descendant enrichment options."""

import json

from pydantic import Field

from harborrag_core.base import StrictModel

from .budget import IndexingBudgetLimits


class LLMIndexingOptions(StrictModel):
    enabled: bool = False
    prohibited: bool = False
    spending_paused: bool = False
    budgets: IndexingBudgetLimits = Field(default_factory=IndexingBudgetLimits)

    @property
    def serves_enrichment(self) -> bool:
        return self.enabled and not self.prohibited


class TenantIndexingConfig(LLMIndexingOptions):
    """Canonical flattened policy; independent of operator configuration syntax."""

    tenant_id: str = Field(min_length=1, max_length=128)


class IndexingOptions(StrictModel):
    llm: LLMIndexingOptions = Field(default_factory=LLMIndexingOptions)


class TenantIndexingDocument(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    indexing: IndexingOptions = Field(default_factory=IndexingOptions)


def parse_indexing_configuration(payload: str) -> TenantIndexingConfig:
    """Accept indexing.llm.enabled while retaining the canonical admin wire format."""
    value = json.loads(payload)
    if isinstance(value, dict) and "indexing" in value:
        document = TenantIndexingDocument.model_validate(value)
        return TenantIndexingConfig(
            tenant_id=document.tenant_id, **document.indexing.llm.model_dump()
        )
    return TenantIndexingConfig.model_validate(value)


class TenantIndexingState(StrictModel):
    config: TenantIndexingConfig
    epoch: int = Field(default=0, ge=0)
