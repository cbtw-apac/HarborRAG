"""Dev-mode composition: real AppService over dict-backed control-plane fakes.

Fresh, empty fake repositories per call are honest about mock mode having no
persisted data — they exercise the same ports production code depends on,
just with nothing in them yet. Callers may pass seed data to exercise the
read routes without a real database.
"""

from __future__ import annotations

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
from harborrag_runtime.composition import (
    CompositionRoot,
    ControlPlaneRepositories,
    build_in_process_event_bus,
)

from .client import AppService


def mock_app_service(
    *,
    projects: list[Project] | None = None,
    sources: list[SourceConfig] | None = None,
    activity: list[ActivityEntry] | None = None,
    settings: WorkspaceSettings | None = None,
) -> AppService:
    """Build an AppService whose control-plane reads come from in-memory fakes."""
    control_plane = ControlPlaneRepositories(
        projects=FakeProjectRepository({project.id: project for project in projects or []}),
        sources=FakeSourceRepository({source.id: source for source in sources or []}),
        jobs=FakeJobRepository(),
        activity=FakeActivityRepository(list(activity or [])),
        settings=FakeSettingsRepository(settings or WorkspaceSettings()),
        providers=FakeProviderRepository(),
        members=FakeMemberRepository(),
        secrets=FakeSecrets(),
    )
    composition = CompositionRoot(
        control_plane=control_plane,
        event_bus=build_in_process_event_bus(),
        control_db={"ping": "ok", "scheme": "development"},
        mode="development",
    )
    return AppService(composition)
