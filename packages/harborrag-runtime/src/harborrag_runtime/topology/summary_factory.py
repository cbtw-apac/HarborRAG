"""Resolve the summary model without requiring an accepted extraction build."""

from dataclasses import dataclass

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.object_store import (
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_adapters.topology import default_extraction_profile
from harborrag_core.security import AccessContext
from harborrag_core.summaries import SummaryLease, SummaryPolicy
from harborrag_core.topology.budget import IndexingBudgetLimits
from harborrag_core.topology.config import TenantIndexingConfig
from harborrag_runtime.config.graph_build import GraphBuildConfig
from harborrag_runtime.config.settings import RuntimeSettings

from .description_factory import ConfiguredDescriptionGenerator
from .summary_inputs import SummaryInputLoader
from .summary_policy import build_summary_policy
from .summary_service import SummaryProjectionService


@dataclass(frozen=True)
class SummaryRuntimeFactory:
    settings: RuntimeSettings
    control: IngestionControlPlaneDatabase
    reader: ImmutableArtifactReader
    writer: ImmutableArtifactWriter

    def policy(self, tenant_id: str | None = None) -> SummaryPolicy:
        policy = build_summary_policy(self.settings)
        if (
            self.settings.summary_processing_allowed
            and tenant_id == self.settings.ingestion_tenant_id
        ):
            return policy.model_copy(
                update={"processing_policy_revision": self.settings.summary_processing_revision}
            )
        return policy

    async def initialize(self, tenant_id: str) -> None:
        """Apply the configured tenant gate and shared durable budget once at startup."""
        config = GraphBuildConfig.from_settings(self.settings)
        if (
            self.settings.summary_processing_allowed
            and tenant_id == self.settings.ingestion_tenant_id
        ):
            self.control.summaries.allow_shared_processing(
                tenant_id, self.settings.summary_processing_revision
            )
        tenant = next((value for value in config.tenants if value.tenant_id == tenant_id), None)
        if tenant is None:
            return
        await self.control.topology.configure_indexing(
            TenantIndexingConfig(
                tenant_id=tenant_id,
                enabled=tenant.llm_enabled,
                prohibited=tenant.prohibited,
                spending_paused=tenant.spending_paused,
                budgets=IndexingBudgetLimits(**tenant.budget.model_dump()),
            )
        )

    async def synchronize(self, tenant_id: str) -> None:
        config = GraphBuildConfig.from_settings(self.settings)
        tenant = next((value for value in config.tenants if value.tenant_id == tenant_id), None)
        scopes = set(await self.control.summaries.source_scope_ids(tenant_id))
        if tenant:
            scopes.update(value.source_scope_id for value in tenant.sources)
        enabled = self.settings.topology_parent_enabled and not (tenant and tenant.prohibited)
        policy = self.policy(tenant_id) if enabled else None
        if (
            self.settings.summary_processing_allowed
            and tenant_id == self.settings.ingestion_tenant_id
        ):
            allowed = set(
                await self.control.topology.allowed_source_scope_ids(
                    tenant_id,
                    access=AccessContext.system(tenant_id).model_copy(
                        update={"corpus_mode": "tenant_shared"}
                    ),
                )
            )
        elif tenant:
            allowed = {value.source_scope_id for value in tenant.sources if value.enabled}
        else:
            allowed = scopes
        for scope in sorted(scopes):
            await self.control.summaries.configure(
                tenant_id, scope, policy if scope in allowed else None
            )
        await self.control.summaries.configure(
            tenant_id, "@tenant", policy if policy and policy.tenant_enabled else None
        )

    def generator(self, lease: SummaryLease) -> ConfiguredDescriptionGenerator:
        catalog = HarborChatClientConfig.from_file(self.settings.model_config_path)
        if self.policy(lease.tenant_id).fingerprint != lease.policy.fingerprint:
            raise ValueError("summary model configuration changed; synchronize policy before retry")
        profile = default_extraction_profile(catalog, self.settings.topology_parent_model)
        # Explicit model chooses the rollup pin, independently of extraction prompt revisions.
        settings = self.settings.model_copy(update={"topology_parent_model": profile.model})
        return ConfiguredDescriptionGenerator(
            settings,
            profile,
            lease.tenant_id,
            "summary:" + lease.source_scope_id,
            frozen_catalog=catalog,
        )

    def service(self) -> SummaryProjectionService:
        loader = SummaryInputLoader(self.control.summaries, self.reader, self.writer, self.settings)
        return SummaryProjectionService(
            self.control.summaries,
            self.control.topology,
            self.settings,
            self.generator,
            loader.load,
        )
