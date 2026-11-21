"""A malformed ``connection_id`` is a validation error before any lookup, as over HTTP."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harborrag_core.domain.project import Project
from harborrag_runtime.application.control_plane.connection_models import (
    ConnectionKey,
    ConnectionWrite,
)
from harborrag_runtime.config.connectors.models import ConnectionValidationError
from harborrag_runtime.testing.connection_fakes import (
    CONNECTION_KEY,
    JIRA_WRITE,
    ConnectionWorld,
    connection_world,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BAD_KEY = replace(CONNECTION_KEY, connection_id="Jira_Main")


@pytest.fixture
def world() -> ConnectionWorld:
    return connection_world([Project(id="demo", name="DEMO", collection="demo", tenant_id="acme")])


@pytest.fixture
def local_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConnectionWrite:
    """Form values a test checks on disk only, so no check reaches the network."""
    monkeypatch.setenv("HARBORRAG_LOCAL_SOURCE_ROOT", str(tmp_path))
    return ConnectionWrite("local_file", {}, {"source_path": str(tmp_path / "missing")})


async def _call(
    name: str, world: ConnectionWorld, key: ConnectionKey, values: ConnectionWrite
) -> object:
    match name:
        case "get":
            return await world.service.get(key)
        case "put":
            return await world.service.put(key, JIRA_WRITE, actor="u_alex")
        case "test_saved":
            return await world.service.test(key, None)
        case "test_values":
            return await world.service.test(key, values)
        case "delete":
            return await world.service.delete(key, actor="u_alex")
    raise AssertionError(name)


@pytest.mark.parametrize("name", ["get", "put", "test_saved", "test_values", "delete"])
async def test_connection_service_bad_connection_id_is_a_validation_error(
    world: ConnectionWorld, local_values: ConnectionWrite, name: str
) -> None:
    with pytest.raises(ConnectionValidationError) as refused:
        await _call(name, world, _BAD_KEY, local_values)

    assert [issue.field for issue in refused.value.issues] == ["connection_id"]


@pytest.mark.parametrize("name", ["put", "test_values"])
async def test_connection_service_bad_id_in_a_missing_project_is_a_validation_error(
    world: ConnectionWorld, local_values: ConnectionWrite, name: str
) -> None:
    with pytest.raises(ConnectionValidationError) as refused:
        await _call(name, world, replace(_BAD_KEY, project_id="missing"), local_values)

    assert [issue.field for issue in refused.value.issues] == ["connection_id"]
