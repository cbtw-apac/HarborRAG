"""The run listing finds runs by who started them, and a deleted connection's runs by its ref."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from runtime_ingestion.fixtures.release import build_control_plane

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.ingestion.run_contracts import PreparedSourceSubmission
from harborrag_core.ingestion.source_query import ProcessingProfileInput
from harborrag_core.testing.control_plane_fakes import FakeConnectionRepository
from harborrag_engine.control_plane.connection_views import IngestionLinks
from harborrag_engine.ingestion.operations import IngestionOperations
from harborrag_runtime.execution.task_registry import IngestionTaskRegistry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_TENANT = "acme"


def _submission(task_id: str, source_key: str) -> PreparedSourceSubmission:
    return PreparedSourceSubmission(
        task_id=task_id,
        tenant_id=_TENANT,
        connector_name=source_key,
        connector_type="local",
        connection_id=source_key,
        source_scope_id=f"scope-{source_key}",
        configuration_fingerprint="config-v1",
        processing=ProcessingProfileInput(
            parser_profile="parser-v1",
            normalizer_version="normalizer-v1",
            chunk_strategy="chunks-v1",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="sparse-v1",
            graph_projection_version="graph-v1",
        ),
    )


async def _seed(control: IngestionControlPlaneDatabase) -> IngestionOperations:
    """Register four runs whose connections were all deleted without a purge."""
    registry = IngestionTaskRegistry(control)
    await registry.register(_submission("run-1", "demo/tickets"), actor="u_alex", actor_type="user")
    await registry.register(
        _submission("run-2", "demo/tickets"), actor="tickets-hourly", actor_type="schedule"
    )
    await registry.register(_submission("run-3", "demo/wiki"), actor="key_1", actor_type="api_key")
    await registry.register(_submission("run-4", "ops/tickets"))

    async def runs() -> IngestionTaskRegistry:
        return registry

    return IngestionOperations(
        connections=FakeConnectionRepository,
        runs=runs,
        links=IngestionLinks(None, "default", None),
        actions=cast(Any, None),
    )


async def _listed(operations: IngestionOperations, **filters: str) -> dict[str, dict[str, Any]]:
    query: dict[str, Any] = {
        "project_id": None,
        "connection_ref": None,
        "status": None,
        "trigger": None,
        "schedule_id": None,
        "created_after": None,
        "cursor": None,
        "limit": 20,
    }
    page = await operations.list(tenant_id=_TENANT, **(query | filters))
    return {item["ingestion_id"]: item for item in page["items"]}


async def test_a_deleted_connections_runs_are_listed_under_its_ref_and_project(
    tmp_path: Path,
) -> None:
    async with build_control_plane(tmp_path) as control:
        operations = await _seed(control)

        by_ref = await _listed(operations, connection_ref="demo/tickets")
        by_project = await _listed(operations, project_id="demo")
        other_project = await _listed(operations, project_id="ops")

    assert set(by_ref) == {"run-1", "run-2"}
    assert {(run["project_id"], run["connection_id"]) for run in by_ref.values()} == {
        ("demo", "tickets")
    }
    assert set(by_project) == {"run-1", "run-2", "run-3"}
    assert set(other_project) == {"run-4"}


async def test_the_trigger_filters_follow_who_the_registry_recorded(tmp_path: Path) -> None:
    async with build_control_plane(tmp_path) as control:
        operations = await _seed(control)

        user = await _listed(operations, trigger="user")
        api_key = await _listed(operations, trigger="api_key")
        hourly = await _listed(operations, schedule_id="tickets-hourly")
        scheduled_in_demo = await _listed(operations, project_id="demo", trigger="schedule")

    assert set(user) == {"run-1", "run-4"}
    assert set(api_key) == {"run-3"}
    assert set(hourly) == {"run-2"}
    assert hourly["run-2"]["triggered_by"] == {
        "principal_type": "schedule",
        "principal_id": "tickets-hourly",
    }
    assert set(scheduled_in_demo) == {"run-2"}
