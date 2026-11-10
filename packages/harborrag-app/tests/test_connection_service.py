"""Connection use cases: put, test, sync and delete against in-memory stores."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from app_test_connections import (
    CONNECTION_KEY,
    JIRA_WRITE,
    ConnectionWorld,
    connection_world,
    running_task,
)

from harborrag_app.workflow_control.control_plane.connection_views import ANY_VERSION
from harborrag_app.workflow_control.control_plane.connections import (
    ConnectionKey,
    ConnectionWrite,
    WriteOrigin,
)
from harborrag_app.workflow_control.control_plane.effect_recovery import (
    recover_pending_control_plane_effects,
)
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborCapabilityError,
    HarborImmutableFieldError,
    HarborNotFoundError,
    HarborPreconditionFailedError,
)
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import IngestionTaskState
from harborrag_runtime.config.connectors.models import ConnectionValidationError
from harborrag_runtime.ingestion.connections import ConnectionUnavailableError


@pytest.fixture
def world() -> ConnectionWorld:
    return connection_world([Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme")])


@pytest.mark.asyncio
async def test_put_creates_a_connection_and_reads_secrets_back_as_status(
    world: ConnectionWorld,
) -> None:
    result = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")

    view = result.connection
    assert (result.created, result.version) == (True, 1)
    assert view["connection_ref"] == "cpm/cpm-tickets" and view["name"] == "CPM tickets"
    assert view["credentials"]["api_token"] == {
        "set": True,
        "last4": "9f2c",
        "updated_at": view["credentials"]["api_token"]["updated_at"],
        "from": "stored",
    }
    assert view["credentials"]["base_url"] == "https://acme.atlassian.net"
    assert view["stats"]["documents"] == 7
    assert view["stats"]["errors"] == 0
    assert (view["created_by"], view["updated_by"]) == ("u_huy", "u_huy")
    assert "sentinel" not in repr(view)
    entries = await world.control_plane.activity.list(tenant_ids=None)
    assert entries[0].verb == "created" and "sentinel" not in entries[0].summary


@pytest.mark.asyncio
async def test_put_overwrites_keeping_rotating_and_clearing_secrets(world: ConnectionWorld) -> None:
    first = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")
    kept = await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite(
            "jira", {k: v for k, v in JIRA_WRITE.credentials.items() if k != "api_token"}, {}
        ),
        actor="u_ops",
        expected_version=first.version,
    )
    assert (kept.created, kept.version) == (False, 2)
    assert kept.connection["credentials"]["api_token"]["last4"] == "9f2c"
    assert kept.connection["config"] == {}
    assert kept.connection["name"] == "cpm-tickets"

    await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite("jira", {**JIRA_WRITE.credentials, "api_token": "rotated-token-abcd"}, {}),
        actor="u_ops",
    )
    assert list(world.secrets.values.values()) == ["rotated-token-abcd"]

    with pytest.raises(ConnectionValidationError, match="does not match"):
        await world.service.put(
            CONNECTION_KEY,
            ConnectionWrite("jira", {**JIRA_WRITE.credentials, "api_token": None}, {}),
            actor="x",
        )


@pytest.mark.asyncio
async def test_put_preconditions_and_immutable_fields(world: ConnectionWorld) -> None:
    with pytest.raises(HarborPreconditionFailedError):
        await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", expected_version=1)
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")

    with pytest.raises(HarborPreconditionFailedError):
        await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", expected_version=9)
    with pytest.raises(HarborPreconditionFailedError):
        await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", create_only=True)
    with pytest.raises(HarborImmutableFieldError) as immutable:
        await world.service.put(
            CONNECTION_KEY,
            ConnectionWrite("confluence", JIRA_WRITE.credentials, {"space_key": "ENG"}),
            actor="u",
        )
    assert immutable.value.details == {"field": "source_type"}


@pytest.mark.asyncio
async def test_invalid_writes_store_no_secret(world: ConnectionWorld) -> None:
    with pytest.raises(ConnectionValidationError) as unknown:
        await world.service.put(CONNECTION_KEY, ConnectionWrite("notion", {}, {}), actor="u")
    assert unknown.value.issues[0].field == "source_type"

    with pytest.raises(ConnectionValidationError) as invalid:
        await world.service.put(
            CONNECTION_KEY,
            ConnectionWrite("jira", {**JIRA_WRITE.credentials, "base_url": "nope"}, {}),
            actor="u",
        )
    assert invalid.value.issues[0].field == "credentials.base_url"

    with pytest.raises(HarborNotFoundError, match="project"):
        await world.service.put(ConnectionKey("acme", "missing", "x"), JIRA_WRITE, actor="u")
    assert world.secrets.values == {}


@pytest.mark.asyncio
async def test_a_rotated_secret_waits_for_the_running_ingestion(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    world.runs.active = running_task("ing_running", IngestionTaskState.RUNNING)

    await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite("jira", {**JIRA_WRITE.credentials, "api_token": "rotated-token-abcd"}, {}),
        actor="u",
    )
    assert len(world.secrets.values) == 2

    await recover_pending_control_plane_effects(
        world.control_plane, connection_busy=world.service.connection_busy
    )
    assert len(world.secrets.values) == 2

    world.runs.active = None
    await recover_pending_control_plane_effects(
        world.control_plane, connection_busy=world.service.connection_busy
    )
    assert list(world.secrets.values.values()) == ["rotated-token-abcd"]


@pytest.mark.asyncio
async def test_sync_starts_one_ingestion_at_a_time(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")

    ingestion = await world.service.sync(CONNECTION_KEY, actor="u_huy", mode="force")

    assert ingestion["ingestion_id"] == "ing_1"
    assert (ingestion["tenant_id"], ingestion["project_id"], ingestion["connection_id"]) == (
        "acme",
        "cpm",
        "cpm-tickets",
    )
    assert ingestion["status"] == "queued"
    assert ingestion["triggered_by"] == {"principal_type": "user", "principal_id": "u_huy"}
    command = world.submitter.commands[0]
    assert (command.connection_id, command.force_reprocess) == ("cpm/cpm-tickets", True)

    world.runs.active = world.runs.tasks["ing_1"]
    with pytest.raises(HarborAlreadyRunningError) as running:
        await world.service.sync(CONNECTION_KEY, actor="u")
    assert running.value.details == {"ingestion_id": "ing_1"}

    with pytest.raises(HarborCapabilityError):
        await world.service.sync(CONNECTION_KEY, actor="u", item_refs=("ENG-1",))


@pytest.mark.asyncio
async def test_put_with_sync_now_returns_the_ingestion(world: ConnectionWorld) -> None:
    result = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", sync_now=True)

    assert result.ingestion is not None and result.ingestion["ingestion_id"] == "ing_1"


@pytest.mark.asyncio
async def test_delete_removes_documents_then_secrets_then_the_row(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    world.runs.active = running_task("ing_running", IngestionTaskState.RUNNING)
    with pytest.raises(HarborAlreadyRunningError):
        await world.service.delete(CONNECTION_KEY, actor="u")
    world.runs.active = None

    job_id = await world.service.delete(CONNECTION_KEY, actor="u", purge=True)
    with pytest.raises(ConnectionUnavailableError):
        await world.service.sync(CONNECTION_KEY, actor="u")
    await world.service.aclose()

    assert world.removal.calls == [
        {"tenant_id": "acme", "source_key": "cpm/cpm-tickets", "purge": True}
    ]
    assert world.secrets.values == {}
    with pytest.raises(HarborNotFoundError):
        await world.service.get(CONNECTION_KEY)
    job = await world.control_plane.jobs.get(job_id, tenant_ids=None)
    assert job is not None and job.status == "succeeded"


@pytest.mark.asyncio
async def test_a_failed_removal_keeps_the_row_deleting_and_resumes(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    world.removal.fail = True

    job_id = await world.service.delete(CONNECTION_KEY, actor="u")
    await world.service.aclose()

    job = await world.control_plane.jobs.get(job_id, tenant_ids=None)
    assert job is not None and (job.status, job.last_error) == ("failed", "RuntimeError")
    view, _ = await world.service.get(CONNECTION_KEY)
    assert view["status"] == "deleting"

    assert await world.service.resume_deletions() == 1
    with pytest.raises(HarborNotFoundError):
        await world.service.get(CONNECTION_KEY)


@pytest.mark.asyncio
async def test_testing_form_values_saves_nothing(
    world: ConnectionWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARBORRAG_LOCAL_SOURCE_ROOT", str(tmp_path))
    missing = await world.service.test(
        CONNECTION_KEY,
        ConnectionWrite("local_file", {}, {"source_path": str(tmp_path / "missing")}),
    )
    assert missing["ok"] is False and missing["error_category"] == "unreachable"

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide", encoding="utf-8")
    healthy = await world.service.test(
        CONNECTION_KEY, ConnectionWrite("local_file", {}, {"source_path": str(tmp_path / "docs")})
    )
    assert healthy["ok"] is True
    assert [check["name"] for check in healthy["checks"]][0] == "schema"
    assert len(healthy["preview"]["sample"]) == 1

    with pytest.raises(ConnectionValidationError):
        await world.service.test(CONNECTION_KEY, ConnectionWrite("local_file", {}, {}))
    with pytest.raises(HarborNotFoundError):
        await world.service.test(CONNECTION_KEY, None)
    assert (await world.control_plane.connections.list(tenant_ids=None))[0] == []
    assert world.secrets.values == {}


@pytest.mark.asyncio
async def test_list_and_searchable_hide_nothing_but_secrets(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")

    items, cursor = await world.service.list_page(tenant_id="acme")
    chips = await world.service.searchable(tenant_id="acme", project_id="cpm")

    assert [item["connection_ref"] for item in items] == ["cpm/cpm-tickets"] and cursor is None
    assert chips == [
        {
            "connection_ref": "cpm/cpm-tickets",
            "connection_id": "cpm-tickets",
            "source_type": "jira",
            "name": "CPM tickets",
            "documents": 7,
        }
    ]
    assert (await world.service.list_page(tenant_id="other"))[0] == []
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_changed_endpoint_never_reuses_a_stored_secret(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    moved = ConnectionWrite("jira", {"base_url": "https://attacker.example", "email": "x@y.z"}, {})

    with pytest.raises(ConnectionValidationError) as on_put:
        await world.service.put(CONNECTION_KEY, moved, actor="u")
    with pytest.raises(ConnectionValidationError) as on_test:
        await world.service.test(CONNECTION_KEY, moved)

    for caught in (on_put, on_test):
        assert [issue.as_dict() for issue in caught.value.issues] == [
            {"field": "credentials.api_token", "issue": "send it again: base_url changed"}
        ]
    same_host = await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite(
            "jira", {"base_url": "https://acme.atlassian.net/", "email": "bot@acme.com"}, {}
        ),
        actor="u",
    )
    assert same_host.connection["credentials"]["api_token"]["last4"] == "9f2c"


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["stored", "env"])
async def test_a_reference_sent_back_does_not_carry_a_secret_to_a_new_host(
    world: ConnectionWorld, held: str
) -> None:
    if held == "env":
        operator_write = ConnectionWrite(
            "jira", {**JIRA_WRITE.credentials, "api_token": {"secret_ref": "env:JIRA_TOKEN"}}, {}
        )
        await world.service.put(CONNECTION_KEY, operator_write, actor="ops", origin="operator")
    else:
        await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    stored = await world.control_plane.connections.get(
        tenant_id="acme", project_id="cpm", connection_id="cpm-tickets"
    )
    assert stored is not None
    echoed = {"secret_ref": stored.credentials["api_token"]["secret_ref"]}
    moved = ConnectionWrite(
        "jira", {"base_url": "https://attacker.example", "email": "x@y.z", "api_token": echoed}, {}
    )

    with pytest.raises(ConnectionValidationError) as on_put:
        await world.service.put(CONNECTION_KEY, moved, actor="u")
    with pytest.raises(ConnectionValidationError) as on_test:
        await world.service.test(CONNECTION_KEY, moved)

    for caught in (on_put, on_test):
        assert [issue.as_dict() for issue in caught.value.issues] == [
            {"field": "credentials.api_token", "issue": "send it again: base_url changed"}
        ]
    same_host = await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite("jira", {**JIRA_WRITE.credentials, "api_token": echoed}, {}),
        actor="u",
    )
    assert same_host.connection["credentials"]["api_token"]["from"] == held


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["api", "operator"])
async def test_a_test_cannot_borrow_another_connections_stored_secret(
    world: ConnectionWorld, origin: WriteOrigin
) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    stored = await world.control_plane.connections.get(
        tenant_id="acme", project_id="cpm", connection_id="cpm-tickets"
    )
    assert stored is not None
    borrowed = ConnectionWrite(
        "jira",
        {
            "base_url": "https://attacker.example",
            "email": "x@y.z",
            "api_token": {"secret_ref": stored.credentials["api_token"]["secret_ref"]},
        },
        {},
    )

    with pytest.raises(ConnectionValidationError) as caught:
        await world.service.test(ConnectionKey("acme", "cpm", "brand-new"), borrowed, origin=origin)

    assert [issue.field for issue in caught.value.issues] == ["credentials.api_token"]
    assert "stored references are not supported" in caught.value.issues[0].issue


@pytest.mark.asyncio
async def test_switching_auth_method_drops_and_retires_the_old_secret(
    world: ConnectionWorld,
) -> None:
    key = ConnectionKey("acme", "cpm", "handbook")
    first = await world.service.put(
        key,
        ConnectionWrite("github", {"method": "pat", "token": "ghp_secret_1234"}, _REPO),
        actor="u",
    )
    stored_ref = first.connection["credentials"]["token"]

    public = await world.service.put(
        key, ConnectionWrite("github", {"method": "none"}, _REPO), actor="u"
    )

    assert stored_ref["set"] is True
    assert public.connection["credentials"] == {"method": "none"}
    assert world.secrets.values == {}


@pytest.mark.asyncio
async def test_a_secret_status_sent_back_keeps_the_stored_value(world: ConnectionWorld) -> None:
    first = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    echoed = {**JIRA_WRITE.credentials, "api_token": first.connection["credentials"]["api_token"]}

    again = await world.service.put(CONNECTION_KEY, ConnectionWrite("jira", echoed, {}), actor="u")

    assert again.connection["credentials"]["api_token"]["last4"] == "9f2c"
    assert len(world.secrets.values) == 1


@pytest.mark.asyncio
async def test_a_failed_sync_now_still_saves_and_reports_why(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    world.runs.active = running_task("ing_0")

    result = await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite("jira", JIRA_WRITE.credentials, {}, name="Renamed"),
        actor="u",
        sync_now=True,
    )

    assert result.ingestion is None and result.version == 2
    assert isinstance(result.sync_error, HarborAlreadyRunningError)
    assert result.connection["name"] == "Renamed"


@pytest.mark.asyncio
async def test_if_match_star_matches_any_existing_version(world: ConnectionWorld) -> None:
    with pytest.raises(HarborPreconditionFailedError):
        await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", expected_version=ANY_VERSION)
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")

    updated = await world.service.put(
        CONNECTION_KEY, replace(JIRA_WRITE, name="Renamed"), actor="u", expected_version=ANY_VERSION
    )

    assert updated.version == 2


@pytest.mark.asyncio
async def test_a_write_that_changes_nothing_stores_nothing(world: ConnectionWorld) -> None:
    first = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")
    stored = dict(world.secrets.values)

    again = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_ops")

    assert (again.created, again.version) == (False, 1)
    assert again.connection["updated_at"] == first.connection["updated_at"]
    assert again.connection["updated_by"] == "u_huy"
    assert again.connection["credentials"] == first.connection["credentials"]
    assert world.secrets.values == stored
    entries = await world.control_plane.activity.list(tenant_ids=None)
    assert [entry.verb for entry in entries] == ["created"]


@pytest.mark.asyncio
async def test_sending_the_same_token_again_clears_needs_reauth(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    await world.control_plane.connections.set_status(
        tenant_id="acme", source_key="cpm/cpm-tickets", status="needs_reauth"
    )
    stored = dict(world.secrets.values)

    again = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")

    assert again.connection["status"] == "active"
    assert again.version == 2
    assert world.secrets.values == stored


@pytest.mark.asyncio
async def test_an_unchanged_write_still_starts_sync_now(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")

    again = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", sync_now=True)

    assert again.version == 1
    assert again.ingestion is not None and again.sync_error is None


@pytest.mark.asyncio
async def test_settings_the_connector_refuses_are_field_errors(world: ConnectionWorld) -> None:
    with pytest.raises(ConnectionValidationError) as caught:
        await world.service.put(
            ConnectionKey("acme", "cpm", "site"),
            ConnectionWrite(
                "sharepoint",
                {"method": "pat", "access_token": "eyJ0eXAi-token"},
                {"site_id": "s1", "item_ids": ["a"], "root_path": "Docs"},
            ),
            actor="u",
        )

    assert caught.value.issues[0].field == "config.item_ids"
    assert "item_ids and root_path" in caught.value.issues[0].issue
    assert world.secrets.values == {}


_REPO = {"owner": "acme", "repo": "handbook"}


@pytest.mark.asyncio
async def test_only_operator_writes_may_set_an_env_reference(world: ConnectionWorld) -> None:
    by_reference = ConnectionWrite(
        "jira",
        {**JIRA_WRITE.credentials, "api_token": {"secret_ref": "env:HARBOR_EMBED_API_KEY"}},
        {},
    )

    with pytest.raises(ConnectionValidationError) as on_put:
        await world.service.put(CONNECTION_KEY, by_reference, actor="u")
    with pytest.raises(ConnectionValidationError) as on_test:
        await world.service.test(CONNECTION_KEY, by_reference)
    operator = await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite(
            "jira", {**JIRA_WRITE.credentials, "api_token": {"secret_ref": "env:JIRA_TOKEN"}}, {}
        ),
        actor="ops",
        origin="operator",
    )
    renamed = await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite(
            "jira",
            {k: v for k, v in JIRA_WRITE.credentials.items() if k != "api_token"},
            {},
            name="Renamed",
        ),
        actor="u",
    )

    for caught in (on_put, on_test):
        assert caught.value.issues[0].field == "credentials.api_token"
        assert "connectors.yaml or with the harborrag CLI" in caught.value.issues[0].issue
    assert operator.connection["credentials"]["api_token"]["from"] == "env"
    assert renamed.connection["credentials"]["api_token"]["from"] == "env"


_DELEGATED = {"method": "oauth", "tenant_id": "entra-tenant", "client_id": "entra-app"}
_SITE = {"site_url": "https://acme.sharepoint.com/sites/knowledge"}


@pytest.mark.asyncio
async def test_only_operators_set_the_sharepoint_token_cache_path(world: ConnectionWorld) -> None:
    key = ConnectionKey("acme", "cpm", "knowledge")
    with_path = ConnectionWrite(
        "sharepoint", {**_DELEGATED, "token_cache_path": "/var/lib/harborrag/kb.json"}, _SITE
    )
    moved = ConnectionWrite(
        "sharepoint", {**_DELEGATED, "token_cache_path": "/etc/harborrag.json"}, _SITE
    )

    with pytest.raises(ConnectionValidationError) as on_put:
        await world.service.put(key, with_path, actor="u")
    with pytest.raises(ConnectionValidationError) as on_test:
        await world.service.test(key, with_path)
    await world.service.put(key, with_path, actor="ops", origin="operator")
    echoed = await world.service.put(key, with_path, actor="u")
    with pytest.raises(ConnectionValidationError) as on_change:
        await world.service.put(key, moved, actor="u")
    with pytest.raises(ConnectionValidationError) as on_omit:
        await world.service.put(key, ConnectionWrite("sharepoint", _DELEGATED, _SITE), actor="u")

    for caught in (on_put, on_test, on_change, on_omit):
        assert [issue.field for issue in caught.value.issues] == ["credentials.token_cache_path"]
    assert echoed.connection["credentials"]["token_cache_path"] == "/var/lib/harborrag/kb.json"


@pytest.mark.asyncio
async def test_a_connection_test_never_retries_and_runs_on_its_own_threads(
    world: ConnectionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from harborrag_app.workflow_control.control_plane import connections as module
    from harborrag_runtime.config.errors import ConnectorConfigurationError

    seen: dict[str, Any] = {}

    def record(source_type: str, **parts: Any) -> Any:
        seen.update(tuning=parts["tuning"], thread=threading.current_thread().name)
        raise ConnectorConfigurationError("stop after recording")

    monkeypatch.setattr(module, "build_connector_from_parts", record)
    slow = ConnectionWrite(
        "jira",
        JIRA_WRITE.credentials,
        JIRA_WRITE.config,
        tuning={"max_retries": 5, "request_timeout_seconds": 120},
    )

    await world.service.test(CONNECTION_KEY, slow)

    assert seen["tuning"]["max_retries"] == 0
    assert seen["tuning"]["request_timeout_seconds"] == 10.0
    assert seen["thread"].startswith("connection-test")


@pytest.mark.asyncio
async def test_a_sync_now_that_breaks_still_saves_and_reports_why(
    world: ConnectionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(command: Any, *, idempotency_key: str | None) -> dict[str, object]:
        raise RuntimeError("temporal client exploded")

    monkeypatch.setattr(world.submitter, "submit", broken)

    result = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", sync_now=True)

    assert result.created and result.ingestion is None
    assert type(result.sync_error).__name__ == "HarborUnavailableError"
    assert "temporal" not in str(result.sync_error)
    assert (await world.service.get(CONNECTION_KEY))[1] == 1


@pytest.mark.asyncio
async def test_a_broken_connectors_yaml_does_not_block_submits(
    world: ConnectionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from harborrag_app.workflow_control.ingestion import connection_sources
    from harborrag_core.contracts.errors import HarborUnavailableError
    from harborrag_runtime.ingestion_contracts import SourceSubmission

    imports: list[object] = []

    async def broken_import(path: object, **repositories: object) -> None:
        imports.append(path)
        raise ValueError("connectors.yaml: bad entry")

    monkeypatch.setattr(connection_sources, "import_connectors_once", broken_import)
    no_revisions = replace(world.control_plane, connection_revisions=None)
    builder = connection_sources.ConnectionSourceInputBuilder(
        lambda: no_revisions, prepare=lambda *args, **kwargs: None
    )
    settings = type(
        "Settings", (), {"connector_config_path": "connectors.yaml", "ingestion_tenant_id": "acme"}
    )()
    submission = SourceSubmission(
        task_id="t",
        tenant_id="acme",
        connector_name="cpm/cpm-tickets",
        connection_id="cpm/cpm-tickets",
    )

    for _ in range(2):
        with pytest.raises(HarborUnavailableError, match="revisions are not configured"):
            await builder(settings, submission)  # type: ignore[arg-type]

    assert imports == ["connectors.yaml"]


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


def _racing(world: ConnectionWorld, monkeypatch: pytest.MonkeyPatch, times: int) -> list[int]:
    """Let another write land just before each of the next ``times`` saves."""
    store = world.control_plane.connections
    original = type(store).put
    raced: list[int] = []

    async def put(self: Any, connection: Any, **kwargs: Any) -> Any:
        if len(raced) < times:
            raced.append(1)
            current = await self.get(
                tenant_id=connection.tenant_id,
                project_id=connection.project_id,
                connection_id=connection.connection_id,
            )
            if current is not None:
                await original(self, current)
        return await original(self, connection, **kwargs)

    monkeypatch.setattr(type(store), "put", put)
    return raced


@pytest.mark.asyncio
async def test_a_save_without_if_match_retries_once_after_a_concurrent_write(
    world: ConnectionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    raced = _racing(world, monkeypatch, times=1)
    renamed = ConnectionWrite(
        "jira",
        {k: v for k, v in JIRA_WRITE.credentials.items() if k != "api_token"},
        JIRA_WRITE.config,
        name="Renamed",
    )

    result = await world.service.put(CONNECTION_KEY, renamed, actor="u")

    assert len(raced) == 1
    assert result.connection["name"] == "Renamed"
    assert result.connection["credentials"]["api_token"]["last4"] == "9f2c"


@pytest.mark.asyncio
async def test_a_save_that_keeps_losing_the_race_answers_conflict_and_leaks_no_secret(
    world: ConnectionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harborrag_core.contracts.errors import HarborConflictError, HarborPreconditionFailedError

    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    stored_before = dict(world.secrets.values)
    _racing(world, monkeypatch, times=2)
    rotated = ConnectionWrite(
        "jira", {**JIRA_WRITE.credentials, "api_token": "ATATT3xFfGF0-rotated-1a2b"}, {}
    )

    with pytest.raises(HarborConflictError) as caught:
        await world.service.put(CONNECTION_KEY, rotated, actor="u")

    assert not isinstance(caught.value, HarborPreconditionFailedError)
    assert world.secrets.values == stored_before
