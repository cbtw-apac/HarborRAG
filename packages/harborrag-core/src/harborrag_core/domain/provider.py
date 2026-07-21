"""Model provider registration (providers registry).

config carries non-secret settings; the API key lives behind the secrets
port and only its secret_ref appears here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .validation import require_id

ProviderFamily = Literal["chat", "embedding", "reranker"]


@dataclass(slots=True)
class Provider:
    """One configured model provider for a family (chat/embedding/reranker)."""

    id: str
    name: str
    family: ProviderFamily
    config: dict[str, Any] = field(default_factory=dict)
    secret_ref: str | None = None

    def __post_init__(self) -> None:
        require_id(self.id, label="Provider")
