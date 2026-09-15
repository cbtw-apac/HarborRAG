"""Accepted canonical builds authorize each independently retriable derived product."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_topology_service_v2 import enable_v2
from topology_service_support import Harness

from harborrag_core.base import utc_now
from harborrag_core.models.embed import HarborEmbedding, HarborEmbedResponse
from harborrag_core.topology.derived import ContextualIndexProfile, DescriptionOutput
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.topology.contextual import ContextualMaterializer, ContextualResources
from harborrag_runtime.topology.derived import DerivedEnrichmentCoordinator, DerivedResources
from harborrag_runtime.topology.derived_models import (
    DerivedBudget,
    DescriptionArtifacts,
    FrozenDescriptionGenerator,
    FrozenEmbedder,
)
from harborrag_runtime.topology.derived_projection import DerivedVectorProjection
from harborrag_runtime.topology.description_factory import ConfiguredDescriptionGenerator
from harborrag_runtime.topology.parent_materializer import ParentMaterializer


class EmbedModel:
    def __init__(self):
        self.calls = []
        self.before_call = None
        self.fail_at = None

    async def aembed(self, request):
        self.calls.append(request)
        if self.before_call:
            await self.before_call()
        if len(self.calls) == self.fail_at:
            raise RuntimeError("embedding provider unavailable")
        return HarborEmbedResponse(
            embeddings=(HarborEmbedding(index=0, value=(1.0, 0.0)),),
            logical_model="embed",
            embedding_space="test",
            provider="fake",
            provider_model="embed",
            deployment="test",
            request_id="request",
        )


class DescriptionModel:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def generate(self, packets):
        self.calls.append(packets)
        if self.fail:
            raise RuntimeError("description provider unavailable")
        return DescriptionOutput(
            description="Harbor and its connected services.",
            cited_packet_ids=tuple(packet.packet_id for packet in packets),
            complete=True,
        )


class VectorStore:
    def __init__(self):
        self.rows = {}
        self.before_write = None

    async def ensure_index(self, spec, *, context):
        assert spec.index_name.startswith(("contextual-v2-", "parent-v2-"))

    async def upsert_records(self, index_name, records, *, context):
        if self.before_write:
            await self.before_write()
        self.rows.setdefault(index_name, {}).update({row.id: row for row in records})

    async def get_records(self, index_name, point_ids, *, context):
        return [self.rows[index_name][key] for key in point_ids]


class ParentGraph:
    def __init__(self):
        self.parents = {}
        self.valid = True

    async def write_parents(self, build, parents, *, context):
        self.parents[build.build_id] = parents

    async def verify_parents(self, build, parents, *, context):
        return self.valid and self.parents.get(build.build_id) == parents


class DerivedHarness:
    def __init__(self, harness, build):
        self.embed = EmbedModel()
        self.descriptions = DescriptionModel()
        self.vectors = VectorStore()
        self.graph = ParentGraph()
        profile = ContextualIndexProfile(model="embed", dimension=2, deployment_revision="r1")
        budget = DerivedBudget(
            harness.control.topology, "DEFAULT", build.build_id, Decimal("0.001")
        )
        artifacts = DescriptionArtifacts(harness.reader, harness.writer, profile.fingerprint)
        resources = ContextualResources(
            FrozenEmbedder(self.embed, artifacts, "DEFAULT", build.build_id),
            harness.writer,
            harness.reader,
        )
        self.resources = DerivedResources(
            harness.control.topology,
            ContextualMaterializer(resources, profile),
            ParentMaterializer(resources, profile),
            FrozenDescriptionGenerator(self.descriptions, budget, artifacts),
            DerivedVectorProjection(self.vectors, harness.reader),
            self.graph,
        )
        self.coordinator = DerivedEnrichmentCoordinator(self.resources)


async def accepted(harness):
    await enable_v2(harness)
    await harness.publish(extra_content=("Another connected service.",))
    result = await harness.service.run_once("DEFAULT")
    assert result.state == "accepted"
    build = await harness.control.topology.get_build("DEFAULT", result.build_id)
    job = await harness.control.topology.get_job("DEFAULT", result.job_id)
    return job, build, tuple(harness.model.inputs)


async def revoke(harness, job):
    now = utc_now()
    await harness.control.topology.set_permissions(
        ResolvedPermissionSnapshot(
            tenant_id="DEFAULT",
            resource_kind="document",
            resource_id=job.document_id,
            revision="acl-revoked",
            resolved_at=now,
            expires_at=now + timedelta(hours=1),
            known=True,
            processing_allowed=False,
            public=False,
        )
    )


@pytest.mark.asyncio
async def test_accepted_build_publishes_both_views_and_reuses_frozen_model_work(tmp_path):
    async with Harness(tmp_path) as h:
        job, build, inputs = await accepted(h)
        d = DerivedHarness(h, build)
        assert await d.coordinator.complete(job, build, inputs) == {
            "contextual": "ready",
            "parents": "ready",
        }
        rows = await h.control.topology.active_derivations("DEFAULT", access=h.access)
        assert {row.lineage.artifact_kind for row in rows} == {
            "contextual_chunk",
            "parent_summary",
            "parent_graph_view",
            "parent_description",
        }
        by_kind = {row.lineage.artifact_kind: row.lineage for row in rows}
        parent_profile = d.resources.parents.profile.parent_fingerprint
        assert by_kind["parent_summary"].metadata["summary_profile"] == parent_profile
        assert by_kind["parent_graph_view"].metadata["summary_profile"] == parent_profile
        assert (
            by_kind["parent_summary"].metadata["summary_digest"]
            == by_kind["parent_graph_view"].metadata["summary_digest"]
        )
        assert len(d.descriptions.calls) == 1
        calls = len(d.embed.calls)
        assert calls >= 3
        assert await d.coordinator.complete(job, build, inputs) == {
            "contextual": "ready",
            "parents": "ready",
        }
        assert len(d.embed.calls) == calls and len(d.descriptions.calls) == 1
        assert await h.control.topology.active_derivations("DEFAULT", access=h.access) == rows
        assert all(
            row.lineage.permission_dependencies == build.permission_dependencies for row in rows
        )


@pytest.mark.asyncio
async def test_parent_model_failure_keeps_contextual_ready_and_primary_accepted(tmp_path):
    async with Harness(tmp_path) as h:
        job, build, inputs = await accepted(h)
        d = DerivedHarness(h, build)
        d.descriptions.fail = True
        assert await d.coordinator.complete(job, build, inputs) == {
            "contextual": "ready",
            "parents": "pending:RuntimeError",
        }
        rows = await h.control.topology.active_derivations("DEFAULT", access=h.access)
        assert [row.lineage.artifact_kind for row in rows] == ["contextual_chunk"]
        assert (await h.control.topology.get_job("DEFAULT", job.job_id)).state == "accepted"


@pytest.mark.asyncio
async def test_contextual_failure_keeps_parent_stage_independent_and_reuses_partial_embeddings(
    tmp_path,
):
    async with Harness(tmp_path) as h:
        job, build, inputs = await accepted(h)
        d = DerivedHarness(h, build)
        d.embed.fail_at = 2
        assert await d.coordinator.complete(job, build, inputs) == {
            "contextual": "pending:RuntimeError",
            "parents": "ready",
        }
        first_request = d.embed.calls[0]
        assert await d.coordinator.complete(job, build, inputs) == {
            "contextual": "ready",
            "parents": "ready",
        }
        assert d.embed.calls.count(first_request) == 1
        assert len(d.descriptions.calls) == 1


@pytest.mark.asyncio
async def test_revocation_during_projection_never_publishes_derived_readiness(tmp_path):
    async with Harness(tmp_path) as h:
        job, build, inputs = await accepted(h)
        d = DerivedHarness(h, build)
        d.vectors.before_write = lambda: revoke(h, job)
        assert await d.coordinator.complete(job, build, inputs) == {
            "contextual": "pending:ValueError",
            "parents": "superseded",
        }
        assert not await h.control.topology.active_derivations("DEFAULT", access=h.access)
        assert not d.descriptions.calls and not d.graph.parents
        assert (await h.control.topology.get_job("DEFAULT", job.job_id)).state == "accepted"


@pytest.mark.asyncio
async def test_revocation_hides_already_ready_views_and_prevents_cached_republication(tmp_path):
    async with Harness(tmp_path) as h:
        job, build, inputs = await accepted(h)
        d = DerivedHarness(h, build)
        assert set((await d.coordinator.complete(job, build, inputs)).values()) == {"ready"}
        await revoke(h, job)
        calls = len(d.embed.calls)
        assert await d.coordinator.complete(job, build, inputs) == {
            "contextual": "superseded",
            "parents": "superseded",
        }
        assert not await h.control.topology.active_derivations("DEFAULT", access=h.access)
        assert len(d.embed.calls) == calls


@pytest.mark.asyncio
async def test_missing_derived_cost_budget_defers_without_revoking_primary_build(tmp_path):
    async with Harness(tmp_path) as h:
        job, build, inputs = await accepted(h)
        d = DerivedHarness(h, build)
        descriptions = replace(
            d.resources.descriptions,
            budget=replace(d.resources.descriptions.budget, cost_ceiling_usd=None),
        )
        coordinator = DerivedEnrichmentCoordinator(replace(d.resources, descriptions=descriptions))
        assert await coordinator.complete(job, build, inputs) == {
            "contextual": "ready",
            "parents": "deferred:provider_cost_ceiling_unconfigured",
        }
        assert not d.descriptions.calls
        assert await h.control.topology.get_build_lineage("DEFAULT", build.build_id)


@pytest.mark.asyncio
async def test_parent_graph_verification_failure_does_not_publish_parent_vectors(tmp_path):
    async with Harness(tmp_path) as h:
        job, build, inputs = await accepted(h)
        d = DerivedHarness(h, build)
        d.graph.valid = False
        assert await d.coordinator.complete(job, build, inputs) == {
            "contextual": "ready",
            "parents": "pending:ValueError",
        }
        assert all(index.startswith("contextual-v2-") for index in d.vectors.rows)


@pytest.mark.asyncio
async def test_parent_embedding_failure_preserves_independent_summary_and_graph_readiness(
    tmp_path,
):
    async with Harness(tmp_path) as h:
        job, build, inputs = await accepted(h)
        d = DerivedHarness(h, build)
        d.embed.fail_at = len(build.chunk_ids) + 1

        assert await d.coordinator.complete(job, build, inputs) == {
            "contextual": "ready",
            "parents": "pending:RuntimeError",
        }
        assert build.build_id in d.graph.parents
        rows = await h.control.topology.active_derivations("DEFAULT", access=h.access)
        assert {row.lineage.artifact_kind for row in rows} == {
            "contextual_chunk",
            "parent_summary",
            "parent_graph_view",
        }


@pytest.mark.asyncio
async def test_parent_configuration_is_lazy_and_not_required_for_frozen_packet_reuse(
    tmp_path, monkeypatch
):
    async with Harness(tmp_path) as h:
        job, build, inputs = await accepted(h)
        d = DerivedHarness(h, build)
        calls = []

        def unavailable(*args, **kwargs):
            calls.append(True)
            raise RuntimeError("parent configuration unavailable")

        monkeypatch.setattr(
            "harborrag_runtime.topology.description_factory.HarborChatClientConfig.from_file",
            unavailable,
        )
        configured = ConfiguredDescriptionGenerator(
            RuntimeSettings(), job.policy.profile, "DEFAULT", job.document_id
        )
        assert not calls
        descriptions = replace(d.resources.descriptions, delegate=configured)
        coordinator = DerivedEnrichmentCoordinator(replace(d.resources, descriptions=descriptions))
        assert await coordinator.complete(job, build, inputs) == {
            "contextual": "ready",
            "parents": "pending:RuntimeError",
        }
        assert len(calls) == 1
        assert set((await d.coordinator.complete(job, build, inputs)).values()) == {"ready"}
        assert set((await coordinator.complete(job, build, inputs)).values()) == {"ready"}
        assert len(calls) == 1  # Frozen description packet bypasses unavailable configuration.
