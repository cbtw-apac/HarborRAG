from __future__ import annotations

from dataclasses import dataclass

from harborrag_adapters.models.runtime.client_dependencies import ModelClientDependencies

from .invocation import RerankInvocation
from .registry import RerankProviderRegistry


@dataclass(frozen=True, slots=True)
class RerankClientDependencies(ModelClientDependencies):
    """Optional adapter boundaries and ownership used to compose one reranking client."""

    invocation: RerankInvocation | None = None
    provider_registry: RerankProviderRegistry | None = None
