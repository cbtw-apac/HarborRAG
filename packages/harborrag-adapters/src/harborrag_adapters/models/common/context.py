from __future__ import annotations

from enum import StrEnum

from harborrag_core.models.context import ModelOperationContext


def update_operation_context(
    context: ModelOperationContext,
    *,
    provider: str | StrEnum | None,
    provider_model: str | None,
    deployment: str | None,
) -> None:
    """Copy provider identity from an adapter object into the stable core context."""

    context.provider = provider.value if isinstance(provider, StrEnum) else provider
    context.provider_model = provider_model
    context.deployment = deployment
