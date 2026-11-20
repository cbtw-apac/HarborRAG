"""How connection-scope setting overrides reach a prepared source submission.

Split out of test_connection_service.py (AC8/AC9/AC10) to keep that module
under the file-length quality gate.
"""

from __future__ import annotations

import pytest
from app_test_connections import CONNECTION_KEY, JIRA_WRITE, ConnectionWorld, connection_world

from harborrag_core.domain.project import Project


@pytest.fixture
def world() -> ConnectionWorld:
    return connection_world([Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme")])


@pytest.mark.asyncio
async def test_ac8_the_connection_scopes_setting_revision_reaches_the_prepared_source(
    world: ConnectionWorld,
) -> None:
    """The run's `settings_revision` is the connection scope's revision, not a fake."""
    from dataclasses import replace

    from harborrag_app.workflow_control.ingestion import connection_sources
    from harborrag_core.testing.control_plane_fakes import (
        FakeSettingOverrideRepository,
        FakeSettingScopeRevisionRepository,
    )
    from harborrag_runtime.ingestion_contracts import SourceSubmission

    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")
    revisions = FakeSettingScopeRevisionRepository()
    await revisions.bump_if_match(
        "acme", "connection", "cpm/cpm-tickets", expected_revision=0, source="api", actor="u_huy"
    )
    control_plane = replace(
        world.control_plane,
        setting_overrides=FakeSettingOverrideRepository(),
        setting_scope_revisions=revisions,
    )

    captured: dict[str, object] = {}

    def capture(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    builder = connection_sources.ConnectionSourceInputBuilder(
        lambda: control_plane, prepare=capture
    )
    settings = type(
        "Settings",
        (),
        {"connector_config_path": "connectors.yaml", "ingestion_tenant_id": "acme"},
    )()
    submission = SourceSubmission(
        task_id="t",
        tenant_id="acme",
        connector_name="cpm/cpm-tickets",
        connection_id="cpm/cpm-tickets",
    )

    await builder(settings, submission)  # type: ignore[arg-type]

    assert captured["settings_revision"] == 1
    assert captured["pdf_engine"] is None
    assert captured["chunking_max_tokens"] is None


@pytest.mark.asyncio
async def test_ac8_an_overridden_pdf_parser_and_max_tokens_reach_the_prepared_source(
    world: ConnectionWorld,
) -> None:
    """Explicit `parsing.pdf_parser`/`chunking.max_tokens` overrides reach the run."""
    from dataclasses import replace

    from harborrag_app.workflow_control.ingestion import connection_sources
    from harborrag_core.domain.setting_override import SettingOverride
    from harborrag_core.testing.control_plane_fakes import (
        FakeSettingOverrideRepository,
        FakeSettingScopeRevisionRepository,
    )
    from harborrag_runtime.ingestion_contracts import SourceSubmission

    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")
    overrides = FakeSettingOverrideRepository()
    for key, value in (("parsing.pdf_parser", "docling"), ("chunking.max_tokens", 256)):
        await overrides.put(
            SettingOverride(
                tenant_id="acme",
                scope_level="connection",
                scope_id="cpm/cpm-tickets",
                key=key,
                value=value,
                created_by="u_huy",
                updated_by="u_huy",
            ),
            expected_version=None,
        )
    revisions = FakeSettingScopeRevisionRepository()
    await revisions.bump_if_match(
        "acme", "connection", "cpm/cpm-tickets", expected_revision=0, source="api", actor="u_huy"
    )
    control_plane = replace(
        world.control_plane, setting_overrides=overrides, setting_scope_revisions=revisions
    )

    captured: dict[str, object] = {}

    def capture(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    builder = connection_sources.ConnectionSourceInputBuilder(
        lambda: control_plane, prepare=capture
    )
    settings = type(
        "Settings",
        (),
        {"connector_config_path": "connectors.yaml", "ingestion_tenant_id": "acme"},
    )()
    submission = SourceSubmission(
        task_id="t",
        tenant_id="acme",
        connector_name="cpm/cpm-tickets",
        connection_id="cpm/cpm-tickets",
    )

    await builder(settings, submission)  # type: ignore[arg-type]

    assert captured["pdf_engine"] == "docling"
    assert captured["chunking_max_tokens"] == 256


@pytest.mark.asyncio
async def test_ac9_ingestion_concurrency_timeout_and_max_attempts_reach_the_prepared_source(
    world: ConnectionWorld,
) -> None:
    """Explicit `ingestion.*` overrides reach the run; unset keys stay None."""
    from dataclasses import replace

    from harborrag_app.workflow_control.ingestion import connection_sources
    from harborrag_core.domain.setting_override import SettingOverride
    from harborrag_core.testing.control_plane_fakes import (
        FakeSettingOverrideRepository,
        FakeSettingScopeRevisionRepository,
    )
    from harborrag_runtime.ingestion_contracts import SourceSubmission

    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")
    overrides = FakeSettingOverrideRepository()
    for key, value in (
        ("ingestion.concurrency", 2),
        ("ingestion.activity_timeout", "PT2M"),
        ("ingestion.max_attempts", 2),
    ):
        await overrides.put(
            SettingOverride(
                tenant_id="acme",
                scope_level="connection",
                scope_id="cpm/cpm-tickets",
                key=key,
                value=value,
                created_by="u_huy",
                updated_by="u_huy",
            ),
            expected_version=None,
        )
    revisions = FakeSettingScopeRevisionRepository()
    await revisions.bump_if_match(
        "acme", "connection", "cpm/cpm-tickets", expected_revision=0, source="api", actor="u_huy"
    )
    control_plane = replace(
        world.control_plane, setting_overrides=overrides, setting_scope_revisions=revisions
    )

    captured: dict[str, object] = {}

    def capture(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    builder = connection_sources.ConnectionSourceInputBuilder(
        lambda: control_plane, prepare=capture
    )
    settings = type(
        "Settings",
        (),
        {"connector_config_path": "connectors.yaml", "ingestion_tenant_id": "acme"},
    )()
    submission = SourceSubmission(
        task_id="t",
        tenant_id="acme",
        connector_name="cpm/cpm-tickets",
        connection_id="cpm/cpm-tickets",
    )

    await builder(settings, submission)  # type: ignore[arg-type]

    assert captured["ingestion_concurrency"] == 2
    assert captured["ingestion_activity_timeout_seconds"] == 120
    assert captured["ingestion_max_attempts"] == 2


@pytest.mark.asyncio
async def test_ac9_unset_ingestion_settings_stay_none(world: ConnectionWorld) -> None:
    """Without an override, `ingestion.*` resolves to None (uses the worker's own defaults)."""
    from harborrag_app.workflow_control.ingestion import connection_sources
    from harborrag_runtime.ingestion_contracts import SourceSubmission

    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")

    captured: dict[str, object] = {}

    def capture(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    builder = connection_sources.ConnectionSourceInputBuilder(
        lambda: world.control_plane, prepare=capture
    )
    settings = type(
        "Settings",
        (),
        {"connector_config_path": "connectors.yaml", "ingestion_tenant_id": "acme"},
    )()
    submission = SourceSubmission(
        task_id="t",
        tenant_id="acme",
        connector_name="cpm/cpm-tickets",
        connection_id="cpm/cpm-tickets",
    )

    await builder(settings, submission)  # type: ignore[arg-type]

    assert captured["ingestion_concurrency"] is None
    assert captured["ingestion_activity_timeout_seconds"] is None
    assert captured["ingestion_max_attempts"] is None


@pytest.mark.asyncio
async def test_ac10_a_running_ingestions_resolved_settings_survive_a_later_patch(
    world: ConnectionWorld,
) -> None:
    """A PATCH after a run already resolved its settings must not rewrite that run.

    Settings are resolved once per submission (same architecture as AC8): the
    object `builder()` already returned for a prior submission is a frozen
    dataclass nobody can mutate, and resolving a *later* submission re-reads
    the scope fresh. This proves the later PATCH changes only the next
    resolution, never one already captured.
    """
    from dataclasses import replace

    from harborrag_app.workflow_control.ingestion import connection_sources
    from harborrag_core.domain.setting_override import SettingOverride
    from harborrag_core.testing.control_plane_fakes import (
        FakeSettingOverrideRepository,
        FakeSettingScopeRevisionRepository,
    )
    from harborrag_runtime.ingestion_contracts import SourceSubmission

    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")
    overrides = FakeSettingOverrideRepository()
    for key, value in (("chunking.max_tokens", 512), ("ingestion.concurrency", 4)):
        await overrides.put(
            SettingOverride(
                tenant_id="acme",
                scope_level="connection",
                scope_id="cpm/cpm-tickets",
                key=key,
                value=value,
                created_by="u_huy",
                updated_by="u_huy",
            ),
            expected_version=None,
        )
    revisions = FakeSettingScopeRevisionRepository()
    await revisions.bump_if_match(
        "acme", "connection", "cpm/cpm-tickets", expected_revision=0, source="api", actor="u_huy"
    )
    control_plane = replace(
        world.control_plane, setting_overrides=overrides, setting_scope_revisions=revisions
    )

    captures: list[dict[str, object]] = []

    def capture(*args: object, **kwargs: object) -> object:
        captures.append(dict(kwargs))
        return object()

    builder = connection_sources.ConnectionSourceInputBuilder(
        lambda: control_plane, prepare=capture
    )
    settings = type(
        "Settings",
        (),
        {"connector_config_path": "connectors.yaml", "ingestion_tenant_id": "acme"},
    )()
    submission = SourceSubmission(
        task_id="running-task",
        tenant_id="acme",
        connector_name="cpm/cpm-tickets",
        connection_id="cpm/cpm-tickets",
    )

    # A sync is already running: its settings were resolved once, at this call.
    await builder(settings, submission)
    running = captures[-1]
    assert running["settings_revision"] == 1
    assert running["chunking_max_tokens"] == 512
    assert running["ingestion_concurrency"] == 4

    # PATCH connection:cpm/cpm-tickets to 256 / 2 while the run above is "in flight".
    for key, value in (("chunking.max_tokens", 256), ("ingestion.concurrency", 2)):
        await overrides.put(
            SettingOverride(
                tenant_id="acme",
                scope_level="connection",
                scope_id="cpm/cpm-tickets",
                key=key,
                value=value,
                created_by="u_huy",
                updated_by="u_huy",
            ),
            expected_version=None,
        )
    await revisions.bump_if_match(
        "acme", "connection", "cpm/cpm-tickets", expected_revision=1, source="api", actor="u_huy"
    )

    # The already-resolved run's captured settings are untouched by the PATCH.
    assert running["settings_revision"] == 1
    assert running["chunking_max_tokens"] == 512
    assert running["ingestion_concurrency"] == 4

    # The next ingestion resolves fresh and sees the new revision and values.
    next_submission = replace(submission, task_id="next-task")
    await builder(settings, next_submission)
    next_run = captures[-1]
    assert next_run["settings_revision"] == 2
    assert next_run["chunking_max_tokens"] == 256
    assert next_run["ingestion_concurrency"] == 2


@pytest.mark.asyncio
async def test_settings_revision_defaults_to_zero_without_a_revisions_port(
    world: ConnectionWorld,
) -> None:
    from dataclasses import replace

    from harborrag_app.workflow_control.ingestion import connection_sources
    from harborrag_runtime.ingestion_contracts import SourceSubmission

    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")
    control_plane = replace(world.control_plane, setting_scope_revisions=None)

    captured: dict[str, object] = {}

    def capture(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    builder = connection_sources.ConnectionSourceInputBuilder(
        lambda: control_plane, prepare=capture
    )
    settings = type(
        "Settings",
        (),
        {"connector_config_path": "connectors.yaml", "ingestion_tenant_id": "acme"},
    )()
    submission = SourceSubmission(
        task_id="t",
        tenant_id="acme",
        connector_name="cpm/cpm-tickets",
        connection_id="cpm/cpm-tickets",
    )

    await builder(settings, submission)  # type: ignore[arg-type]

    assert captured["settings_revision"] == 0
