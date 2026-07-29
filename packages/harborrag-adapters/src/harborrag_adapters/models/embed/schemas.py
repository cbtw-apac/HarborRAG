from __future__ import annotations

from dataclasses import dataclass

from harborrag_adapters.models.runtime.client_dependencies import ModelClientDependencies

from .invocation import EmbeddingInvocation
from .registry import EmbedProviderRegistry


@dataclass(frozen=True, slots=True)
class EmbedClientDependencies(ModelClientDependencies):
    """Optional adapter boundaries and ownership used to compose one embedding client."""

    invocation: EmbeddingInvocation | None = None
    provider_registry: EmbedProviderRegistry | None = None
