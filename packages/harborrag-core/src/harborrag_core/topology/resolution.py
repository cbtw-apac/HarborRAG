"""Explicit, auditable identity decisions; names never authorize a merge."""

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel


class ResolutionRequest(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=128)
    action: Literal["merge", "revert"]
    entity_ids: tuple[str, ...] = Field(default=(), max_length=100)
    reverts_decision_id: str | None = Field(default=None, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    actor: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.action == "merge":
            if len(set(self.entity_ids)) < 2 or self.reverts_decision_id is not None:
                raise ValueError("merge requires at least two distinct entities and no reversal")
        elif self.entity_ids or not self.reverts_decision_id:
            raise ValueError("revert requires a prior decision ID and no entity IDs")
        return self


class ResolutionDecision(StrictModel):
    request: ResolutionRequest
    revision: int = Field(ge=1)
    created_at: datetime
    resolution_revision: str
