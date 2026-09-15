from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_core.topology.derived import ContextualIndexProfile
from harborrag_runtime.topology.derived_dispatch import DerivedDispatcher


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
async def test_sdk_request_adapter_uses_keyword_request():
    from harborrag_core.models.embed import HarborEmbedRequest
    from harborrag_runtime.topology.derived_models import RequestEmbedder

    client = SimpleNamespace(aembed=AsyncMock(return_value="response"))
    request = HarborEmbedRequest(inputs=("source",))
    assert await RequestEmbedder(client).aembed(request) == "response"
    client.aembed.assert_awaited_once_with(request=request)
