"""Enrichment publication/recovery safety without external services or model calls."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from topology_service_support import Harness

from harborrag_core.models.errors import HarborChatStructuredOutputError
from harborrag_core.topology import (
    CanonicalMention,
    EvidenceSpan,
    ExtractedEntity,
    ExtractionOutput,
)
from harborrag_core.topology.records import TopologyBuildContent
from harborrag_core.topology.resolution import ResolutionRequest
from harborrag_runtime.topology.operations import _topology_content_digest
from harborrag_runtime.topology.service import TopologyEnrichmentService, _failure_code

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_failure_code_unwraps_groups_and_sanitized_model_errors():
    error = HarborChatStructuredOutputError(
        "structured response validation failed",
        original_exception=ValueError("private provider output"),
    )
    assert _failure_code(ExceptionGroup("task group", [error])) == "ValueError"


def test_frozen_build_comparison_ignores_database_record_order():
    entity = ExtractedEntity(
        local_id="local",
        name="Harbor",
        entity_type="service",
        span=EvidenceSpan(start=0, end=6, quote="Harbor"),
    )
    common = {
        "tenant_id": "DEFAULT",
        "build_id": "build",
        "document_id": "document",
        "document_version_id": "version",
        "chunk_id": "chunk",
        "observation": entity,
    }
    later = CanonicalMention(mention_id="z", entity_id="entity-z", **common)
    earlier = CanonicalMention(mention_id="a", entity_id="entity-a", **common)
    frozen = TopologyBuildContent(
        build_id="build", job_id="job", chunk_ids=("chunk",), mentions=(later, earlier)
    )
    canonical = frozen.model_copy(update={"mentions": (earlier, later)})
    assert _topology_content_digest(frozen) == _topology_content_digest(canonical)


@pytest.mark.asyncio
async def test_publish_enqueues_enrich_verifies_then_exposes_evidence(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()
        assert len(await h.control.topology.list_jobs("DEFAULT")) == 1
        assert not await h.control.topology.active_mentions("DEFAULT", access=h.access)
        result = await h.service.run_once("DEFAULT")
        assert result.state == "accepted"
        assert result.extracted_chunks == 1
        assert h.model.calls == 1
        mentions = await h.control.topology.active_mentions(
            "DEFAULT", labels=("Harbor",), access=h.access
        )
        assert len(mentions) == 1
        assert mentions[0].build_id == result.build_id
        assert not await h.control.topology.active_mentions("another-tenant", access=h.access)
        assert (await h.service.run_once("DEFAULT")).state == "idle"


@pytest.mark.asyncio
async def test_verification_failure_retries_from_frozen_extraction(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()
        h.graph.valid = False
        failed = await h.service.run_once("DEFAULT")
        assert failed.state == "failed"
        assert not await h.control.topology.active_mentions("DEFAULT", access=h.access)
        h.graph.valid = True
        recovered = await h.service.run_once("DEFAULT")
        assert recovered.state == "accepted"
        assert recovered.reused_chunks == 1
        assert h.model.calls == 1


@pytest.mark.asyncio
async def test_retirement_during_graph_write_never_activates_old_build(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()

        async def retire():
            await h.publish("two")

        h.graph.before_write = retire
        result = await h.service.run_once("DEFAULT")
        assert result.state != "accepted"
        assert not await h.control.topology.active_mentions("DEFAULT", access=h.access)
        h.graph.before_write = None
        assert (await h.service.run_once("DEFAULT")).state == "accepted"
        assert h.model.calls == 1  # identical frozen content and context across versions


@pytest.mark.asyncio
async def test_two_workers_only_one_claims_model_work(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()
        results = await asyncio.gather(h.service.run_once("DEFAULT"), h.service.run_once("DEFAULT"))
        assert sorted(result.state for result in results) == ["accepted", "idle"]
        assert h.model.calls == 1


@pytest.mark.asyncio
async def test_empty_extraction_is_a_successful_checkpoint(tmp_path):
    async with Harness(tmp_path) as h:
        h.model.output = ExtractionOutput(entities=(), assertions=())
        await h.publish()
        result = await h.service.run_once("DEFAULT")
        assert result.state == "accepted"
        assert result.build_id in await h.control.topology.eligible_build_ids(
            "DEFAULT", (result.build_id,), access=h.access
        )
        assert not await h.control.topology.active_mentions("DEFAULT", access=h.access)


@pytest.mark.asyncio
async def test_policy_disable_during_extraction_prevents_exposure(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()

        async def disable():
            await h.control.topology.configure_policy(
                h.policy.model_copy(update={"enabled": False})
            )

        h.graph.before_write = disable
        assert (await h.service.run_once("DEFAULT")).state != "accepted"
        assert not await h.control.topology.active_mentions("DEFAULT", access=h.access)


@pytest.mark.asyncio
async def test_operation_deadline_stops_work_without_acceptance(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()

        async def hang():
            await asyncio.Event().wait()

        h.graph.before_write = hang
        service = TopologyEnrichmentService(h.resources, job_seconds=0.05)
        assert (await service.run_once("DEFAULT")).state == "failed"
        assert not await h.control.topology.active_mentions("DEFAULT", access=h.access)


@pytest.mark.asyncio
async def test_rebuild_checks_frozen_manifest_without_calling_model(tmp_path, monkeypatch):
    from harborrag_runtime.topology import operations

    async with Harness(tmp_path) as h:
        await h.publish()
        result = await h.service.run_once("DEFAULT")
        h.graph.builds.clear()

        @asynccontextmanager
        async def runtime(_settings):
            yield SimpleNamespace(control=h.control, projection=h.graph, artifact_reader=h.reader)

        monkeypatch.setattr(operations, "connect_topology_runtime", runtime)
        rebuilt = await operations.rebuild(None, "DEFAULT", result.build_id)
        assert rebuilt["verified"] and rebuilt["eligible"]
        assert h.model.calls == 1


@pytest.mark.asyncio
async def test_pinned_factory_failure_prevents_model_and_projection(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()

        @asynccontextmanager
        async def changed_config(profile):
            raise ValueError("deployment revision changed")
            yield  # pragma: no cover

        service = TopologyEnrichmentService(replace(h.resources, extractor=changed_config))
        result = await service.run_once("DEFAULT")
        assert result.state == "failed" and result.error_code == "ValueError"
        assert not h.graph.builds
        assert h.model.calls == 0


@pytest.mark.asyncio
async def test_audited_merge_and_revert_rebuild_without_reextraction(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish(item="a")
        await h.service.run_once("DEFAULT")
        original = h.model.output.entities[0]
        h.model.output = h.model.output.model_copy(
            update={
                "entities": (
                    original.model_copy(
                        update={
                            "name": "Harbor Platform",
                            "span": original.span.model_copy(
                                update={"end": 15, "quote": "Harbor Platform"}
                            ),
                        }
                    ),
                )
            }
        )
        await h.publish(item="b", content="Harbor Platform is a service.")
        await h.service.run_once("DEFAULT")
        before = await h.control.topology.active_mentions("DEFAULT", access=h.access)
        identities = tuple(sorted({item.entity_id for item in before}))
        assert len(identities) == 2
        await h.control.topology.record_resolution(
            ResolutionRequest(
                tenant_id="DEFAULT",
                decision_id="merge-1",
                action="merge",
                entity_ids=identities,
                actor="operator",
                reason="Reviewed same service in both documents",
            )
        )
        assert not await h.control.topology.active_mentions("DEFAULT", access=h.access)
        await h.control.topology.reconcile("DEFAULT")
        assert (await h.service.run_once("DEFAULT")).state == "accepted"
        assert (await h.service.run_once("DEFAULT")).state == "accepted"
        merged = await h.control.topology.active_mentions("DEFAULT", access=h.access)
        assert len({item.entity_id for item in merged}) == 1
        assert h.model.calls == 2
        await h.control.topology.record_resolution(
            ResolutionRequest(
                tenant_id="DEFAULT",
                decision_id="revert-1",
                action="revert",
                reverts_decision_id="merge-1",
                actor="operator",
                reason="Different services",
            )
        )
        await h.control.topology.reconcile("DEFAULT")
        await h.service.run_once("DEFAULT")
        await h.service.run_once("DEFAULT")
        restored = await h.control.topology.active_mentions("DEFAULT", access=h.access)
        assert {item.entity_id for item in restored} == set(identities)
        assert h.model.calls == 2


@pytest.mark.asyncio
async def test_cancellation_releases_lease_for_checkpoint_recovery(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish()
        writing = asyncio.Event()

        async def hang():
            writing.set()
            await asyncio.Event().wait()

        h.graph.before_write = hang
        task = asyncio.create_task(h.service.run_once("DEFAULT"))
        await asyncio.wait_for(writing.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await h.control.topology.active_mentions("DEFAULT", access=h.access)
        h.graph.before_write = None
        assert (await h.service.run_once("DEFAULT")).state == "accepted"
        assert h.model.calls == 1


@pytest.mark.asyncio
async def test_corrupted_canonical_chunks_never_reach_model(tmp_path):
    async with Harness(tmp_path) as h:
        version = await h.publish()
        snapshot = await h.control.document_versions.get_version(str(version.document_version_id))
        ref = snapshot.chunk_artifact
        key = ("DEFAULT", ref.bucket, ref.key)
        # Simulate provider corruption without updating the canonical artifact checksum.
        h.store._objects[key] = h.store._objects[key].replace(b"Harbor", b"Hacked")
        assert (await h.service.run_once("DEFAULT")).state == "failed"
        assert h.model.calls == 0
        assert not h.graph.builds


@pytest.mark.asyncio
async def test_navigation_route_records_are_not_extracted_as_evidence(tmp_path):
    async with Harness(tmp_path) as h:
        await h.publish(include_route=True)
        result = await h.service.run_once("DEFAULT")
        assert result.state == "accepted"
        assert result.extracted_chunks == 1
        assert h.model.calls == 1
        build = await h.control.topology.get_build("DEFAULT", result.build_id)
        assert len(build.chunk_ids) == 1
        assert all(chunk_id.startswith("chunk-") for chunk_id in build.chunk_ids)
