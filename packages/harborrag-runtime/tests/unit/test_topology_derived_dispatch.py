import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_core.topology.derived import ContextualIndexProfile
from harborrag_runtime.topology.derived_dispatch import DerivedDispatcher

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_disabled_dispatch_does_not_load_models_or_query_pending(monkeypatch):
    repository, runner = SimpleNamespace(pending_derivation_build_ids=AsyncMock()), AsyncMock()
    dispatcher = DerivedDispatcher(
        repository, runner, SimpleNamespace(topology_derived_enabled=False)
    )
    assert await dispatcher.run_page("tenant") == 0
    repository.pending_derivation_build_ids.assert_not_called()
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_pending_dispatch_carries_both_profiles_and_pages_past_failed_build(monkeypatch):
    profile = ContextualIndexProfile(model="model", dimension=2, deployment_revision="r")
    monkeypatch.setattr(
        "harborrag_runtime.topology.derived_dispatch.build_contextual_profile",
        lambda settings: profile,
    )
    repository = SimpleNamespace(
        pending_derivation_build_ids=AsyncMock(side_effect=[("a", "b"), ()])
    )
    runner = AsyncMock(
        side_effect=[ValueError("unavailable"), {"contextual": "ready", "parents": "ready"}]
    )
    dispatcher = DerivedDispatcher(
        repository,
        runner,
        SimpleNamespace(
            topology_derived_enabled=True,
            topology_job_seconds=5,
        ),
    )
    assert await dispatcher.run_page("tenant", limit=2) == 2
    assert runner.await_count == 2
    assert await dispatcher.run_page("tenant", limit=2) == 0
    calls = repository.pending_derivation_build_ids.await_args_list
    assert calls[0].kwargs["parent_profile"] == profile.parent_fingerprint
    assert calls[0].args == ("tenant", profile.fingerprint)
    assert calls[1].kwargs["after_build_id"] == "b"


@pytest.mark.asyncio
async def test_progress_logs_each_products_state_without_its_detail(monkeypatch, caplog):
    """A deferral's reason stays out of the log; only the state word is kept."""
    profile = ContextualIndexProfile(model="model", dimension=2, deployment_revision="r")
    monkeypatch.setattr(
        "harborrag_runtime.topology.derived_dispatch.build_contextual_profile",
        lambda settings: profile,
    )
    repository = SimpleNamespace(pending_derivation_build_ids=AsyncMock(return_value=("a",)))
    runner = AsyncMock(
        return_value={"parents": "deferred:summary_run_budget", "contextual": "ready"}
    )
    dispatcher = DerivedDispatcher(
        repository,
        runner,
        SimpleNamespace(topology_derived_enabled=True, topology_job_seconds=5),
    )

    with caplog.at_level(logging.INFO, logger="harborrag.runtime.topology"):
        await dispatcher.run_page("tenant")

    (record,) = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "topology.derived_enrichment_progress"
    ]
    assert record.__dict__["readiness_status"] == "contextual:ready,parents:deferred"
    assert record.__dict__["build_id"] == "a"
    assert "summary_run_budget" not in record.getMessage()


@pytest.mark.asyncio
async def test_sdk_request_adapter_uses_keyword_request():
    from harborrag_core.models.embed import HarborEmbedRequest
    from harborrag_runtime.topology.derived_models import RequestEmbedder

    client = SimpleNamespace(aembed=AsyncMock(return_value="response"))
    request = HarborEmbedRequest(inputs=("source",))
    assert await RequestEmbedder(client).aembed(request) == "response"
    client.aembed.assert_awaited_once_with(request=request)
