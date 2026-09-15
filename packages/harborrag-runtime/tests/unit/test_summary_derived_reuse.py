"""Independent projection retries consume accepted text instead of calling a model."""

from dataclasses import replace
from functools import partial

import pytest
from test_summary_projection_service import Model, service
from test_topology_derived_coordinator import DerivedHarness, accepted
from topology_service_support import Harness

from harborrag_core.summaries import SummaryPolicy
from harborrag_runtime.topology.derived import DerivedEnrichmentCoordinator
from harborrag_runtime.topology.summary_products import summary_parents


@pytest.mark.asyncio
async def test_projection_retry_reuses_accepted_summary_cards(tmp_path):
    async with Harness(tmp_path) as harness:
        harness.include_graph = True
        job, build, inputs = await accepted(harness)
        await harness.control.summaries.configure(
            "DEFAULT", "scope", SummaryPolicy(model_fingerprint="model", debounce_seconds=0)
        )
        model = Model()
        assert await service(harness, model).run_once("DEFAULT") == "current"
        calls = len(model.calls)
        derived = DerivedHarness(harness, build)
        derived.descriptions.fail = True
        coordinator = DerivedEnrichmentCoordinator(
            replace(
                derived.resources,
                parent_loader=partial(summary_parents, harness.control, "DEFAULT", build),
            )
        )
        derived.graph.valid = False
        first = await coordinator.complete(job, build, inputs)
        assert first["contextual"] == "ready" and first["parents"] != "ready"
        derived.graph.valid = True
        assert await coordinator.complete(job, build, inputs) == {
            "contextual": "ready",
            "parents": "ready",
        }
        assert not derived.descriptions.calls
        assert len(model.calls) == calls
