from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTokenUsage(BaseModel):
    """Provide the stable total-token field shared by model usage schemas."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_tokens: int = Field(default=0, ge=0)
