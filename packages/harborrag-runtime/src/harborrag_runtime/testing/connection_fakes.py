"""In-memory doubles for the connection and project service tests.

Test support only: nothing in production imports this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from harborrag_adapters.connectors.policies.network import DestinationPolicy
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.domain.project import Project
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.ingestion import IngestionTask, IngestionTaskState, TaskDocumentPage, TaskPage
from harborrag_core.security import AccessContext
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
from harborrag_engine.control_plane.connection_views import IngestionLinks
from harborrag_engine.ingestion.models import IngestionCreateCommand
from harborrag_engine.ingestion.operations import IngestionOperations
from harborrag_memory.tiers.in_memory import InMemoryConversationMemory
from harborrag_runtime.application.control_plane.connection_models import (
    ConnectionKey,
    ConnectionWrite,
)
from harborrag_runtime.application.control_plane.connections import ConnectionService
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.settings import RuntimeSettings

CONNECTION_KEY = ConnectionKey(tenant_id="acme", project_id="demo", connection_id="demo-tickets")
JIRA_WRITE = ConnectionWrite(
    source_type="jira",
    name="DEMO tickets",
    credentials={
        "base_url": "https://acme.atlassian.net",
        "email": "bot@acme.com",
        "api_token": "ATATT3xFfGF0-sentinel-9f2c",
    },
    config={"jql": "project = DEMO"},
)


@dataclass
class Runs:
    """In-memory ingestion runs keyed by connection."""

    active: IngestionTask | None = None
    tasks: dict[str, IngestionTask] = field(default_factory=dict)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    progress_calls: list[str] = field(default_factory=list)
    batch_calls: list[tuple[str, ...]] = field(default_factory=list)
    scopes: dict[str, list[str]] = field(default_factory=dict)

    async def latest_for_connection(self, *, tenant_id: str, connection_id: str) -> Any:
        """Return the scripted active ingestion."""
        return self.active

    async def active_for_connection(self, *, tenant_id: str, connection_id: str) -> Any:
        """Return the scripted active ingestion."""
        return self.active

    async def connection_document_count(self, *, tenant_id: str, connection_id: str) -> int:
        """Return a fixed document count."""
        return 7

    async def get(self, task_id: str) -> IngestionTask | None:
        """Return the stored task, or None."""
        return self.tasks.get(task_id)

    async def progress(self, task_id: str) -> dict[str, int]:
        """Return the scripted progress and remember the call."""
        self.progress_calls.append(task_id)
        return self.counts.get(task_id, {"published": 1})

    async def progress_for_tasks(self, task_ids: Sequence[str]) -> dict[str, dict[str, int]]:
        """Return the scripted progress per task and remember the call."""
        self.batch_calls.append(tuple(task_ids))
        return {task_id: self.counts.get(task_id, {"published": 1}) for task_id in task_ids}

    async def source_scope_ids_by_connection(
        self, *, tenant_id: str, connection_ids: Sequence[str]
    ) -> dict[str, list[str]]:
        """Return the scripted source scopes of each connection, ``[]`` for none."""
        return {
            connection_id: self.scopes.get(connection_id, []) for connection_id in connection_ids
        }

    async def list_tasks(self, **_filters: Any) -> TaskPage:
        """Return every stored task as one page."""
        return TaskPage(tuple(self.tasks.values()), has_more=False)

    async def document_results_page(self, task_id: str, **_filters: Any) -> TaskDocumentPage:
        """Return an empty document page."""
        return TaskDocumentPage((), has_more=False)


@dataclass
class SourceGrants:
    """``SourceScopeAuthorizer`` over the source scopes scripted for each principal.

    Args:
        readable: Source scopes each principal may read.
        over_budget: Refuse every call as the database does past its enumeration budget.
        requests: Every access context asked about.
        closed: Whether ``aclose`` ran.
    """

    readable: dict[str, tuple[str, ...]] = field(default_factory=dict)
    over_budget: bool = False
    requests: list[AccessContext] = field(default_factory=list)
    closed: bool = False

    async def allowed_source_scope_ids(
        self, tenant_id: str, *, access: AccessContext, limit: int = 10000
    ) -> tuple[str, ...]:
        """Return the principal's scripted scopes, or refuse past the budget."""
        self.requests.append(access)
        if self.over_budget:
            raise HarborConflictError("authorized source enumeration exceeds the configured budget")
        return self.readable.get(access.principal_id, ())

    async def aclose(self) -> None:
        """Remember the close."""
        self.closed = True


@dataclass
class Submitter:
    """Records submitted ingestions and registers them as pending tasks."""

    runs: Runs
    commands: list[IngestionCreateCommand] = field(default_factory=list)
    ended: set[str] = field(default_factory=set)

    async def submit(
        self, command: IngestionCreateCommand, *, idempotency_key: str | None
    ) -> dict[str, object]:
        """Record the command and return a running task."""
        self.commands.append(command)
        task_id = f"ing_{len(self.commands)}"
        self.runs.tasks[task_id] = running_task(
            task_id,
            force=command.force_reprocess,
            actor=command.actor,
            actor_type=command.actor_type,
        )
        return {"task_id": task_id, "status": "PENDING"}

    async def settle_if_ended(self, task_id: str) -> bool:
        """End a run listed in ``ended``, as one whose workflow closed unrecorded."""
        if task_id not in self.ended:
            return False
        if self.runs.active is not None and self.runs.active.task_id == task_id:
            self.runs.active = None
        return True


@dataclass
class Removal:
    """Removal double that can fail once."""

    fail: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def remove(self, **kwargs: Any) -> None:
        """Record the call; fail once when configured to."""
        self.calls.append(kwargs)
        if self.fail:
            self.fail = False
            raise RuntimeError("vector store down")


def running_task(
    task_id: str,
    status: IngestionTaskState = IngestionTaskState.PENDING,
    *,
    force: bool = False,
    actor: str | None = None,
    actor_type: str | None = None,
) -> IngestionTask:
    """Build an ingestion task for the sample connection in the given state."""
    return IngestionTask(
        task_id=task_id,
        source_scope_id="scope-1",
        status=status,
        request={
            "tenant_id": "acme",
            "connection_id": "demo/demo-tickets",
            "force_reprocess": force,
            **({"actor": actor, "actor_type": actor_type or "user"} if actor else {}),
        },
    )


@dataclass
class ConnectionWorld:
    """A ``ConnectionService`` over fakes, with the doubles it was built from."""

    service: ConnectionService
    control_plane: ControlPlaneRepositories
    secrets: FakeSecrets
    runs: Runs
    submitter: Submitter
    removal: Removal
    grants: SourceGrants

    @property
    def operations(self) -> IngestionOperations:
        """Run reads over the world's connections and runs."""

        async def provide_runs() -> Runs:
            return self.runs

        return IngestionOperations(
            connections=lambda: self.control_plane.connections,
            runs=provide_runs,
            links=IngestionLinks(None, "default", None),
            actions=self.submitter,  # type: ignore[arg-type]
        )


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
        secrets=secrets,
        pending_effects=FakePendingEffectRepository(),
        leases=FakeLeaseRepository(),
        graph_conflicts=FakeGraphConflictRepository(),
        connection_revisions=FakeConnectionRevisionRepository(),
    )


def connection_world(
    projects: list[Project], *, settings: RuntimeSettings | None = None
) -> ConnectionWorld:
    """Build a ``ConnectionService`` over fakes, with its doubles exposed.

    Args:
        projects: Projects the control plane holds.
        settings: Settings that choose each tenant's corpus mode; the defaults when ``None``.
    """
    secrets = FakeSecrets()
    runs = Runs()
    removal = Removal()
    submitter = Submitter(runs)
    grants = SourceGrants()
    control_plane = fake_control_plane(projects, secrets)

    async def provide_runs() -> Runs:
        return runs

    service = ConnectionService(
        control_plane=lambda: control_plane,
        runs=provide_runs,
        ingestions=submitter,
        removal=lambda: removal,  # type: ignore[arg-type,return-value]
        destination_policy=DestinationPolicy(),
        instance_id="test",
        authorizer=grants,
        settings=settings or RuntimeSettings(),
    )
    return ConnectionWorld(service, control_plane, secrets, runs, submitter, removal, grants)
