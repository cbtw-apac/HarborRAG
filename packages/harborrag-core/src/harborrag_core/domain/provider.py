"""Model provider registration (providers registry).

config carries non-secret settings; the API key lives behind the secrets
port and only its secret_ref appears here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .validation import require_id, require_tenant_id, validate_secret_free_config

ProviderFamily = Literal["chat", "embedding", "reranker"]


@dataclass(slots=True)
class Provider:
    """One configured model provider for a family (chat/embedding/reranker).

    ``deleted_at`` makes delete a tombstone, not a row removal: a
    ``routing_rules`` row may reference this provider's id via a DB foreign
    key, so the row must keep existing for that reference to stay valid.
    A non-``None`` value means the provider is deleted -- repositories hide
    it from ``list``/``get`` accordingly.
    """

    id: str
    name: str
    family: ProviderFamily
    tenant_id: str
    config: dict[str, Any] = field(default_factory=dict)
    secret_ref: str | None = None
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        require_id(self.id, label="Provider")
        require_tenant_id(self.tenant_id)
        validate_secret_free_config(self.config)
