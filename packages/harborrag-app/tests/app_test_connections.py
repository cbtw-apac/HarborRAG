"""In-memory doubles for the connection and project service tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harborrag_app.workflow_control.control_plane.connections import (
    ConnectionKey,
    ConnectionService,
    ConnectionWrite,
)
from harborrag_app.workflow_control.ingestion.models import IngestionCreateCommand
from harborrag_core.domain.project import Project
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.testing.control_plane_fakes import (
    FakeActivityRepository,
    FakeConnectionRepository,
    FakeConnectionRevisionRepository,
    FakeGraphConflictRepository,
    FakeJobRepository,
    FakeLeaseRepository,
    FakeMemberRepository,
    FakePendingEffectRepository,
    FakeProjectRepository,
    FakeProviderRepository,
    FakeSettingsRepository,
)
from harborrag_core.testing.fakes import FakeSecrets
from harborrag_runtime.agent import InMemoryAgentRunRepository
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.memory import InMemoryConversationMemory

CONNECTION_KEY = ConnectionKey(tenant_id="acme", project_id="cpm", connection_id="cpm-tickets")
JIRA_WRITE = ConnectionWrite(
    source_type="jira",
    name="CPM tickets",
    credentials={
        "base_url": "https://acme.atlassian.net",
        "email": "bot@acme.com",
        "api_token": "ATATT3xFfGF0-sentinel-9f2c",
    },
    config={"jql": "project = CPM"},
)


@dataclass
class Runs:
    """In-memory ingestion runs keyed by connection."""

    active: IngestionTask | None = None
    tasks: dict[str, IngestionTask] = field(default_factory=dict)

    async def latest_for_connection(self, *, tenant_id: str, connection_id: str) -> Any:
        return self.active

    async def active_for_connection(self, *, tenant_id: str, connection_id: str) -> Any:
        return self.active

    async def connection_document_count(self, *, tenant_id: str, connection_id: str) -> int:
        return 7

    async def get(self, task_id: str) -> IngestionTask | None:
        return self.tasks.get(task_id)

    async def progress(self, task_id: str) -> dict[str, int]:
        return {"published": 1}


@dataclass
class Submitter:
    """Records submitted ingestions and registers them as pending tasks."""

    runs: Runs
    commands: list[IngestionCreateCommand] = field(default_factory=list)

    async def submit(
        self, command: IngestionCreateCommand, *, idempotency_key: str | None
    ) -> dict[str, object]:
        self.commands.append(command)
        task_id = f"ing_{len(self.commands)}"
        self.runs.tasks[task_id] = running_task(task_id)
        return {"task_id": task_id, "status": "PENDING"}


@dataclass
class Removal:
    """Removal double that can fail once."""

    fail: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def remove(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.fail:
            self.fail = False
            raise RuntimeError("vector store down")


def running_task(
    task_id: str, status: IngestionTaskState = IngestionTaskState.PENDING
) -> IngestionTask:
    return IngestionTask(
        task_id=task_id,
        source_scope_id="scope-1",
        status=status,
        request={"tenant_id": "acme", "connection_id": "cpm/cpm-tickets"},
    )


@dataclass
class ConnectionWorld:
    service: ConnectionService
    control_plane: ControlPlaneRepositories
    secrets: FakeSecrets
    runs: Runs
    submitter: Submitter
    removal: Removal


def fake_control_plane(projects: list[Project], secrets: FakeSecrets) -> ControlPlaneRepositories:
    """Control-plane repositories over in-memory fakes."""
    return ControlPlaneRepositories(
        projects=FakeProjectRepository({project.id: project for project in projects}),
        connections=FakeConnectionRepository(),
        jobs=FakeJobRepository(),
        activity=FakeActivityRepository(),
        settings=FakeSettingsRepository(WorkspaceSettings(tenant_id="acme")),
        providers=FakeProviderRepository(),
        members=FakeMemberRepository(),
        conversation_memory=InMemoryConversationMemory(),
        agent_runs=InMemoryAgentRunRepository(),
        secrets=secrets,
        pending_effects=FakePendingEffectRepository(),
        leases=FakeLeaseRepository(),
        graph_conflicts=FakeGraphConflictRepository(),
        connection_revisions=FakeConnectionRevisionRepository(),
    )


def connection_world(projects: list[Project]) -> ConnectionWorld:
    """A ``ConnectionService`` over fakes, with its doubles exposed."""
    secrets = FakeSecrets()
    runs = Runs()
    removal = Removal()
    submitter = Submitter(runs)
    control_plane = fake_control_plane(projects, secrets)

    async def provide_runs() -> Runs:
        return runs

    service = ConnectionService(
        control_plane=lambda: control_plane,
        runs=provide_runs,
        ingestions=submitter,
        removal=lambda: removal,  # type: ignore[arg-type,return-value]
        instance_id="test",
    )
    return ConnectionWorld(service, control_plane, secrets, runs, submitter, removal)
