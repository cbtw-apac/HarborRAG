from __future__ import annotations

from pydantic import Field

from harborrag_core.base import StrictModel


class ModelTokenUsage(StrictModel):
    """Provide the stable total-token field shared by model usage schemas."""

    total_tokens: int = Field(default=0, ge=0)
