"""Fused windows and post-acceptance derivations retain independent readiness."""

from dataclasses import replace
from decimal import Decimal

import pytest
from topology_service_support import Harness

from harborrag_adapters.topology.extractor import EXTRACTION_PROMPT
from harborrag_core.topology import ChunkExtractionInput, ExtractionProfile, digest
from harborrag_core.topology.budget import IndexingBudgetLimits
from harborrag_core.topology.config import TenantIndexingConfig
from harborrag_core.topology.ontology import builtin_ontology
from harborrag_runtime.topology.budgeted_extractor import extraction_attempt_operation_key
from harborrag_runtime.topology.service import TopologyEnrichmentService


def test_extraction_operation_cap_is_stable_per_attempt_and_retryable_across_attempts():
    value = ChunkExtractionInput(chunk_id="chunk", content="Evidence.")
    profile = ExtractionProfile(model="model", deployment_revision="r1", prompt_digest="p1")

    first = extraction_attempt_operation_key(value, profile, attempt=1)
    assert first == extraction_attempt_operation_key(value, profile, attempt=1)
    assert first != extraction_attempt_operation_key(value, profile, attempt=2)
    with pytest.raises(ValueError, match="positive attempt"):
        extraction_attempt_operation_key(value, profile, attempt=0)


async def enable_v2(harness):
    registry = builtin_ontology()
    profile = harness.policy.profile.model_copy(
        update={
            "schema_version": "2",
            "ontology_version": registry.version,
            "ontology": registry,
            "prompt_digest": digest(EXTRACTION_PROMPT),
            "context_policy": "chunk-v2",
            "code_version": "2",
            "max_input_chars": 180,
        }
    )
    harness.policy = harness.policy.model_copy(update={"profile": profile})
    harness.model.adaptive = True
    await harness.control.topology.configure_policy(harness.policy)


@pytest.mark.asyncio
async def test_fused_subwindows_keep_original_chunk_identity_and_all_character_coverage(tmp_path):
    async with Harness(tmp_path) as h:
        await enable_v2(h)
        content = "Harbor data " * 30
        candidate = await h.publish(content=content)
        result = await h.service.run_once("DEFAULT")
        assert result.state == "accepted"
        assert result.extracted_chunks == 1 and h.model.calls > 1
        assert {value.chunk_id for value in h.model.inputs} == {
            f"chunk-{candidate.document_version_id}"
        }
        assert "".join(value.content for value in h.model.inputs) == content
        assert len({value.window_start for value in h.model.inputs}) == h.model.calls
        build = await h.control.topology.get_build("DEFAULT", result.build_id)
        assert build.projection_revision == "semantic-v2"
        assert len(build.chunk_ids) == len(build.representations) == 1
        assert build.representations[0].description and build.representations[0].retrieval_context
        assert build.permission_dependencies


@pytest.mark.asyncio
async def test_failed_later_window_reuses_frozen_prior_window_on_retry(tmp_path):
    async with Harness(tmp_path) as h:
        await enable_v2(h)
        await h.publish(content="Harbor data " * 30)
        extract = h.model.extract
        fail_once = True

        async def fail_second(value, **kwargs):
            nonlocal fail_once
            if h.model.calls == 1 and fail_once:
                fail_once = False
                raise RuntimeError("provider failed after first frozen window")
            return await extract(value, **kwargs)

        h.model.extract = fail_second
        assert (await h.service.run_once("DEFAULT")).state == "failed"
        assert h.model.calls == 1 and not h.graph.builds
        recovered = await h.service.run_once("DEFAULT")
        assert recovered.state == "accepted"
        assert len({value.window_start for value in h.model.inputs}) == h.model.calls
        assert "".join(value.content for value in h.model.inputs) == "Harbor data " * 30


@pytest.mark.asyncio
async def test_missing_cost_policy_defers_before_provider_dispatch(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()
        service = TopologyEnrichmentService(replace(h.resources, require_budget=True))
        result = await service.run_once("DEFAULT")
        assert result.state == "deferred"
        assert result.error_code == "provider_cost_ceiling_unconfigured"
        assert h.model.calls == 0 and not h.graph.builds
        job = await h.control.topology.get_job("DEFAULT", result.job_id)
        assert job.state == "deferred"
        assert job.available_at is None
        assert not await h.control.topology.active_mentions("DEFAULT", access=h.access)


@pytest.mark.asyncio
async def test_shared_daily_budget_denial_defers_without_model_or_graph_writes(tmp_path):
    async with Harness(tmp_path) as h:
        await h.control.topology.configure_indexing(
            TenantIndexingConfig(
                tenant_id="DEFAULT",
                enabled=True,
                budgets=IndexingBudgetLimits(daily_token_cap=0),
            )
        )
        await h.publish()
        service = TopologyEnrichmentService(
            replace(
                h.resources,
                require_budget=True,
                llm_operation_cost_usd=Decimal("0.001"),
            )
        )
        result = await service.run_once("DEFAULT")
        assert result.state == "deferred" and result.error_code == "daily_tokens"
        assert h.model.calls == 0 and not h.graph.builds
        job = await h.control.topology.get_job("DEFAULT", result.job_id)
        assert job.state == "deferred"
        assert job.available_at is not None


@pytest.mark.asyncio
async def test_derived_failure_does_not_revoke_accepted_extraction(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()

        async def broken_derivation(tenant_id, build_id):
            assert tenant_id == "DEFAULT"
            assert await h.control.topology.get_build_lineage(tenant_id, build_id)
            raise RuntimeError("derived storage unavailable")

        service = TopologyEnrichmentService(replace(h.resources, derive=broken_derivation))
        result = await service.run_once("DEFAULT")
        assert result.state == "accepted"
        assert result.derived == {"contextual": "pending:RuntimeError", "parents": "pending"}
        job = await h.control.topology.get_job("DEFAULT", result.job_id)
        assert job.state == "accepted"
        assert await h.control.topology.active_mentions("DEFAULT", access=h.access)


@pytest.mark.asyncio
async def test_derived_budget_deferral_retains_independent_accepted_state(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()

        async def deferred(tenant_id, build_id):
            return {"contextual": "deferred:daily_tokens", "parents": "pending"}

        result = await TopologyEnrichmentService(replace(h.resources, derive=deferred)).run_once(
            "DEFAULT"
        )
        assert (
            result.state == "accepted" and result.derived["contextual"] == "deferred:daily_tokens"
        )
        assert await h.control.topology.get_build_lineage("DEFAULT", result.build_id)
        assert h.model.calls == 1
