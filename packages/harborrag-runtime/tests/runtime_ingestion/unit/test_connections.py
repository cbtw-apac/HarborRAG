"""Per-run connectors built from stored connections."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harborrag_adapters.connectors.exceptions import AuthenticationError
from harborrag_adapters.parsers import HarborParserFactory
from harborrag_core.domain.connection import ConnectionBuilder
from harborrag_core.testing.control_plane_fakes import (
    FakeConnectionRepository,
    FakeConnectionRevisionRepository,
)
from harborrag_core.testing.fakes import FakeSecrets
from harborrag_runtime.config.connectors.models import ConnectionValidationError
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.connections import SecretUnavailableError, default_secret_resolver
from harborrag_runtime.ingestion.connections import (
    ConnectionHealth,
    ConnectionNotFoundError,
    ConnectionResolver,
    ConnectionUnavailableError,
    ConnectorPool,
    build_connector,
)
from runtime_ingestion.connection_support import connection_row


class _Closing:
    """Stand-in connector that records ``close``."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def stores() -> tuple[FakeConnectionRepository, FakeConnectionRevisionRepository]:
    return FakeConnectionRepository(), FakeConnectionRevisionRepository()


async def _github(
    connections: FakeConnectionRepository, *, branch: str = "main", **changes: Any
) -> None:
    row = connection_row(
        "github",
        source_key="handbook",
        connection_id="handbook",
        credentials={"method": "pat", "token": {"secret_ref": "env:TEST_GITHUB_TOKEN"}},
        config={"owner": "acme", "repo": "handbook", "branch": branch},
        **changes,
    )
    existing = await connections.get_by_source_key(tenant_id="tenant-1", source_key="handbook")
    if existing is not None:
        row = ConnectionBuilder.from_existing(existing).config(row.config).build()
    await connections.put(row)


def _pool(revisions: FakeConnectionRevisionRepository, **kwargs: Any) -> ConnectorPool:
    return ConnectorPool(
        revisions=revisions,
        secrets=default_secret_resolver(FakeSecrets(), environ={"TEST_GITHUB_TOKEN": "ghp_x"}),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_resolver_validates_and_saves_the_run_snapshot(stores: Any) -> None:
    connections, revisions = stores
    await _github(connections)

    resolved = await ConnectionResolver(connections, revisions).resolve(
        tenant_id="tenant-1", source_key="handbook"
    )

    assert resolved.provider == "github"
    assert resolved.fingerprint.startswith("connection-")
    assert await revisions.get(tenant_id="tenant-1", fingerprint=resolved.fingerprint) == (
        resolved.snapshot
    )
    assert resolved.snapshot.secret_refs == ["env:TEST_GITHUB_TOKEN"]


@pytest.mark.asyncio
async def test_resolver_refuses_missing_deleting_and_invalid_connections(stores: Any) -> None:
    connections, revisions = stores
    resolver = ConnectionResolver(connections, revisions)

    with pytest.raises(ConnectionNotFoundError):
        await resolver.resolve(tenant_id="tenant-1", source_key="missing")

    await _github(connections)
    await connections.set_status(tenant_id="tenant-1", source_key="handbook", status="deleting")
    with pytest.raises(ConnectionUnavailableError):
        await resolver.resolve(tenant_id="tenant-1", source_key="handbook")

    await connections.put(
        connection_row("jira", connection_id="broken", config={"jql": "x"}, credentials={})
    )
    with pytest.raises(ConnectionValidationError):
        await resolver.resolve(tenant_id="tenant-1", source_key="broken")


@pytest.mark.asyncio
async def test_a_change_reaches_the_next_run_on_every_worker_without_a_restart(
    stores: Any,
) -> None:
    connections, revisions = stores
    resolver = ConnectionResolver(connections, revisions)
    await _github(connections, branch="main")
    before = await resolver.resolve(tenant_id="tenant-1", source_key="handbook")
    first_worker, second_worker = _pool(revisions), _pool(revisions)
    running = await first_worker.connector(
        tenant_id="tenant-1", task_id="run-1", configuration_fingerprint=before.fingerprint
    )

    await _github(connections, branch="release")
    after = await resolver.resolve(tenant_id="tenant-1", source_key="handbook")

    assert after.fingerprint != before.fingerprint
    for worker in (first_worker, second_worker):
        next_run = await worker.connector(
            tenant_id="tenant-1", task_id="run-2", configuration_fingerprint=after.fingerprint
        )
        assert next_run.provider.config.branch == "release"
    still_running = await second_worker.connector(
        tenant_id="tenant-1", task_id="run-1", configuration_fingerprint=before.fingerprint
    )
    assert running.provider.config.branch == "main"
    assert still_running.provider.config.branch == "main"


@pytest.mark.asyncio
async def test_activities_of_one_run_share_a_connector_and_each_run_gets_its_own(
    stores: Any,
) -> None:
    connections, revisions = stores
    await _github(connections)
    resolved = await ConnectionResolver(connections, revisions).resolve(
        tenant_id="tenant-1", source_key="handbook"
    )
    pool = _pool(revisions)
    fingerprint = resolved.fingerprint

    discovery = await pool.connector(
        tenant_id="tenant-1", task_id="run-1", configuration_fingerprint=fingerprint
    )
    fetch = await pool.connector(
        tenant_id="tenant-1", task_id="run-1", configuration_fingerprint=fingerprint
    )
    second_sync = await pool.connector(
        tenant_id="tenant-1", task_id="run-2", configuration_fingerprint=fingerprint
    )

    assert discovery is fetch
    assert second_sync is not discovery
    assert second_sync.provider.config.token == "ghp_x"


@pytest.mark.asyncio
async def test_the_pool_closes_connectors_it_evicts_and_on_shutdown(
    stores: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections, revisions = stores
    await _github(connections)
    resolved = await ConnectionResolver(connections, revisions).resolve(
        tenant_id="tenant-1", source_key="handbook"
    )
    built: list[_Closing] = []

    def _fake_build(*_args: object, **_kwargs: object) -> _Closing:
        built.append(_Closing())
        return built[-1]

    monkeypatch.setattr("harborrag_runtime.ingestion.connections.build_connector", _fake_build)
    pool = _pool(revisions, capacity=1)

    for task_id in ("run-1", "run-2"):
        await pool.connector(
            tenant_id="tenant-1", task_id=task_id, configuration_fingerprint=resolved.fingerprint
        )
    assert [connector.closed for connector in built] == [True, False]

    await pool.aclose()
    assert built[1].closed is True


@pytest.mark.asyncio
async def test_the_pool_refuses_unknown_snapshots_and_unreadable_secrets(stores: Any) -> None:
    connections, revisions = stores
    await _github(connections)
    resolved = await ConnectionResolver(connections, revisions).resolve(
        tenant_id="tenant-1", source_key="handbook"
    )
    without_env = ConnectorPool(
        revisions=revisions, secrets=default_secret_resolver(FakeSecrets(), environ={})
    )

    with pytest.raises(ConnectorConfigurationError, match="unknown"):
        await _pool(revisions).connector(
            tenant_id="tenant-1", task_id="run", configuration_fingerprint="connection-missing"
        )
    with pytest.raises(SecretUnavailableError, match="TEST_GITHUB_TOKEN"):
        await without_env.connector(
            tenant_id="tenant-1", task_id="run", configuration_fingerprint=resolved.fingerprint
        )


def test_build_connector_rejects_extensions_no_parser_can_read(tmp_path: Path) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    from runtime_ingestion.connection_support import resolved_connection

    resolved = resolved_connection(
        "local_file",
        config={"source_path": str(source), "allowed_extensions": ["md", "xyz", ".RTF"]},
    )

    with pytest.raises(ConnectorConfigurationError) as raised:
        build_connector(resolved.snapshot, {}, parser=HarborParserFactory().create_registry())

    message = str(raised.value)
    assert ".rtf" in message and ".xyz" in message and ".md" not in message


@pytest.mark.asyncio
async def test_health_flags_rejected_credentials_and_clears_after_a_good_run(
    stores: Any,
) -> None:
    connections, _ = stores
    await _github(connections)
    health = ConnectionHealth(connections)

    await health.record_failure(
        tenant_id="tenant-1", source_key="handbook", error=RuntimeError("timeout")
    )
    assert (await _status(connections)) == "active"

    await health.record_failure(
        tenant_id="tenant-1", source_key="handbook", error=AuthenticationError("401")
    )
    assert (await _status(connections)) == "needs_reauth"

    await health.record_success(tenant_id="tenant-1", source_key="handbook")
    assert (await _status(connections)) == "active"

    await connections.set_status(tenant_id="tenant-1", source_key="handbook", status="error")
    await health.record_failure(
        tenant_id="tenant-1", source_key="handbook", error=AuthenticationError("401")
    )
    await health.record_success(tenant_id="tenant-1", source_key="handbook")
    assert (await _status(connections)) == "error"


async def _status(connections: FakeConnectionRepository) -> str:
    row = await connections.get_by_source_key(tenant_id="tenant-1", source_key="handbook")
    assert row is not None
    return row.status
