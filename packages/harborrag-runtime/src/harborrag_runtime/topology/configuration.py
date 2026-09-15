"""Build and synchronize executable graph-build policy from operator config."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.topology import default_extraction_profile, pinned_configuration
from harborrag_core.ports.topology import TopologyRepositoryPort
from harborrag_core.topology import (
    ExtractionProfile,
    TopologyPolicy,
    projection_revision_for_schema,
)
from harborrag_core.topology.budget import IndexingBudgetLimits
from harborrag_core.topology.config import TenantIndexingConfig
from harborrag_runtime.config.graph_build import (
    GraphBuildConfig,
    GraphBuildSourceConfig,
)


class GraphBuildProfileFactory:
    """Lazily pin code-owned extraction contracts to the selected deployment."""

    def __init__(self, model_config_path: str | Path) -> None:
        self._model_config_path = model_config_path
        self._catalog: HarborChatClientConfig | None = None

    def build(self, source: GraphBuildSourceConfig) -> ExtractionProfile:
        if self._catalog is None:
            self._catalog = HarborChatClientConfig.from_file(self._model_config_path)
        profile = default_extraction_profile(self._catalog, source.model).model_copy(
            update={
                "max_input_chars": source.extraction.max_input_chars,
                "max_output_tokens": source.extraction.max_output_tokens,
                "max_windows": source.extraction.max_windows,
            }
        )
        pinned_configuration(self._catalog, profile)
        return profile


@dataclass(frozen=True, slots=True)
class GraphBuildSyncReport:
    tenants: int
    policies_enabled: int
    policies_disabled: int
    documents_enqueued: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class GraphBuildConfigSynchronizer:
    """Idempotently map managed YAML entries onto canonical Postgres controls."""

    def __init__(
        self,
        config: GraphBuildConfig,
        repository: TopologyRepositoryPort,
        profiles: GraphBuildProfileFactory,
    ) -> None:
        self._config = config
        self._repository = repository
        self._profiles = profiles

    async def apply(self) -> GraphBuildSyncReport:
        # Fail before any write if an enabled LLM source cannot be pinned.
        enabled_profiles = {
            (tenant.tenant_id, source.source_scope_id): self._profiles.build(source)
            for tenant in self._config.tenants
            if tenant.llm_enabled
            for source in tenant.sources
            if source.enabled
        }
        enabled = 0
        disabled = 0
        enqueued = 0
        for tenant in self._config.tenants:
            budget = tenant.budget
            await self._repository.configure_indexing(
                TenantIndexingConfig(
                    tenant_id=tenant.tenant_id,
                    enabled=tenant.llm_enabled,
                    prohibited=tenant.prohibited,
                    spending_paused=tenant.spending_paused,
                    budgets=IndexingBudgetLimits(**budget.model_dump()),
                )
            )
            for source in tenant.sources:
                profile = enabled_profiles.get((tenant.tenant_id, source.source_scope_id))
                current = await self._repository.get_policy(
                    tenant.tenant_id, source.source_scope_id
                )
                if profile is not None:
                    await self._repository.configure_policy(
                        TopologyPolicy(
                            tenant_id=tenant.tenant_id,
                            source_scope_id=source.source_scope_id,
                            enabled=True,
                            profile=profile,
                            projection_revision=projection_revision_for_schema(
                                profile.schema_version
                            ),
                        )
                    )
                    enabled += 1
                elif current is not None:
                    await self._repository.configure_policy(
                        current.model_copy(update={"enabled": False})
                    )
                    disabled += 1
            enqueued += await self._repository.reconcile(tenant.tenant_id)
        return GraphBuildSyncReport(len(self._config.tenants), enabled, disabled, enqueued)
