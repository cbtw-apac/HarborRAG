"""Independent projection retries consume accepted text instead of calling a model."""

from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_summary_projection_service import Model, service
from test_topology_derived_coordinator import DerivedHarness, accepted
from topology_service_support import Harness

from harborrag_core.summaries import SummaryPolicy
from harborrag_runtime.topology.budgeted_extractor import EnrichmentDeferredError
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


@pytest.mark.asyncio
async def test_parent_reuse_waits_for_one_complete_document_card():
    build = SimpleNamespace(
        document_id="document",
        document_version_id="version",
        chunk_ids=("chunk",),
    )
    control = SimpleNamespace(
        summaries=SimpleNamespace(document_bindings=AsyncMock(return_value=()))
    )
    with pytest.raises(EnrichmentDeferredError, match="summary_projection_pending"):
        await summary_parents(control, "tenant", build)

    empty_manifest = SimpleNamespace(
        input_chunk_ids=(),
        kind="DocumentVersion",
        input_digest="input",
    )
    binding = SimpleNamespace(
        manifest=empty_manifest,
        card=SimpleNamespace(description="Empty."),
    )
    node = SimpleNamespace(
        entity_type=SimpleNamespace(value="document"),
        logical_id="document",
        section_path=(),
    )
    control.summaries.document_bindings.return_value = ((node, binding),)
    with pytest.raises(EnrichmentDeferredError, match="summary_projection_pending"):
        await summary_parents(control, "tenant", build)
