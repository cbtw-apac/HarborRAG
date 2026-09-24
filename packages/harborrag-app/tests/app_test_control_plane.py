"""Test-only control-plane composition with seedable repositories."""

from __future__ import annotations

from harborrag_app.workflow_control.composition.service import AppService
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.graph_conflict import GraphConflict
from harborrag_core.domain.job import Job
from harborrag_core.domain.mcp_usage import McpConfigSnapshot, McpUsageEntry
from harborrag_core.domain.project import Project
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.routing_rule import RoutingRule
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.ports.control_plane import (
    ActivityRepositoryPort,
    LeaseRepositoryPort,
    PendingEffectRepositoryPort,
)
from harborrag_core.ports.provider_cost import ProviderCostTrackerPort
from harborrag_core.ports.provider_probe import ProviderProbePort
from harborrag_core.ports.secrets import SecretsPort
from harborrag_core.testing.control_plane_fakes import (
    FakeActivityRepository,
    FakeGraphConflictRepository,
    FakeJobRepository,
    FakeLeaseRepository,
    FakeMcpConfigSnapshotRepository,
    FakeMcpQueryLogRepository,
    FakeMemberRepository,
    FakePendingEffectRepository,
    FakeProjectRepository,
    FakeProviderCostTracker,
    FakeProviderProbe,
    FakeProviderRepository,
    FakeRoutingRuleRepository,
    FakeSettingsRepository,
    FakeSourceRepository,
)
from harborrag_core.testing.fakes import FakeSecrets
from harborrag_runtime.agent import InMemoryAgentRunRepository
from harborrag_runtime.composition import CompositionRoot, ControlPlaneRepositories
from harborrag_runtime.memory import InMemoryConversationMemory


def control_plane_app_service(  # noqa: PLR0913 - one seedable kwarg per control-plane repository
    *,
    projects: list[Project] | None = None,
    sources: list[SourceConfig] | None = None,
    jobs: list[Job] | None = None,
    activity: list[ActivityEntry] | None = None,
    activity_repository: ActivityRepositoryPort | None = None,
    settings: WorkspaceSettings | None = None,
    secrets: SecretsPort | None = None,
    pending_effects: PendingEffectRepositoryPort | None = None,
    leases: LeaseRepositoryPort | None = None,
    graph_conflicts: list[GraphConflict] | None = None,
    providers: list[Provider] | None = None,
    routing_rules: list[RoutingRule] | None = None,
    provider_probe: ProviderProbePort | None = None,
    provider_cost: ProviderCostTrackerPort | None = None,
    mcp_query_log_entries: list[McpUsageEntry] | None = None,
    mcp_config_snapshot: McpConfigSnapshot | None = None,
) -> AppService:
    """Build a service with test-only, seedable control-plane repositories.

    ``activity_repository``/``secrets``/``pending_effects``/``leases``/
    ``provider_probe``/``provider_cost`` accept any port implementation, not
    just the stock fakes -- tests that need a repository which fails on
    demand (recoverability coverage) or a canned probe result inject their
    own double.
    """

    control_plane = ControlPlaneRepositories(
        projects=FakeProjectRepository({project.id: project for project in projects or []}),
        sources=FakeSourceRepository({source.id: source for source in sources or []}),
        jobs=FakeJobRepository({job.id: job for job in jobs or []}),
        activity=activity_repository or FakeActivityRepository(list(activity or [])),
        settings=FakeSettingsRepository(settings or WorkspaceSettings(tenant_id="DEFAULT")),
        providers=FakeProviderRepository({provider.id: provider for provider in providers or []}),
        routing_rules=FakeRoutingRuleRepository(list(routing_rules or [])),
        members=FakeMemberRepository(),
        conversation_memory=InMemoryConversationMemory(),
        agent_runs=InMemoryAgentRunRepository(),
        secrets=secrets or FakeSecrets(),
        pending_effects=pending_effects or FakePendingEffectRepository(),
        leases=leases or FakeLeaseRepository(),
        graph_conflicts=FakeGraphConflictRepository(
            {conflict.id: conflict for conflict in graph_conflicts or []}
        ),
        provider_probe=provider_probe or FakeProviderProbe(),
        provider_cost=provider_cost or FakeProviderCostTracker(),
        mcp_query_log=FakeMcpQueryLogRepository(list(mcp_query_log_entries or [])),
        mcp_config_snapshot=FakeMcpConfigSnapshotRepository(mcp_config_snapshot),
    )
    composition = CompositionRoot(
        control_plane=control_plane,
        control_db={"ping": "ok", "scheme": "test"},
        mode="test",
    )
    return AppService(composition)
