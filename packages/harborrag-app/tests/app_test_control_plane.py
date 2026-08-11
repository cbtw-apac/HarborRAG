"""Test-only control-plane composition with seedable repositories."""

from __future__ import annotations

from harborrag_app.workflow_control.composition.service import AppService
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.project import Project
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.testing.fakes import (
    FakeActivityRepository,
    FakeJobRepository,
    FakeMemberRepository,
    FakeProjectRepository,
    FakeProviderRepository,
    FakeSecrets,
    FakeSettingsRepository,
    FakeSourceRepository,
)
from harborrag_runtime.agent import InMemoryAgentRunRepository
from harborrag_runtime.composition import CompositionRoot, ControlPlaneRepositories
from harborrag_runtime.memory import InMemoryConversationMemory


def control_plane_app_service(
    *,
    projects: list[Project] | None = None,
    sources: list[SourceConfig] | None = None,
    activity: list[ActivityEntry] | None = None,
    settings: WorkspaceSettings | None = None,
    secrets: FakeSecrets | None = None,
) -> AppService:
    """Build a service with test-only, seedable control-plane repositories."""

    control_plane = ControlPlaneRepositories(
        projects=FakeProjectRepository({project.id: project for project in projects or []}),
        sources=FakeSourceRepository({source.id: source for source in sources or []}),
        jobs=FakeJobRepository(),
        activity=FakeActivityRepository(list(activity or [])),
        settings=FakeSettingsRepository(settings or WorkspaceSettings(tenant_id="DEFAULT")),
        providers=FakeProviderRepository(),
        members=FakeMemberRepository(),
        conversation_memory=InMemoryConversationMemory(),
        agent_runs=InMemoryAgentRunRepository(),
        secrets=secrets or FakeSecrets(),
    )
    composition = CompositionRoot(
        control_plane=control_plane,
        control_db={"ping": "ok", "scheme": "test"},
        mode="test",
    )
    return AppService(composition)
