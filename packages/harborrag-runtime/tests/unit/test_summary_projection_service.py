"""Publish canonical artifacts, generate independently, re-ingest, and read safely."""

from decimal import Decimal

import pytest
from topology_service_support import Harness

from harborrag_adapters.topology.descriptions import DescriptionRun
from harborrag_core.ingestion import DocumentIdentityBuilder
from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.summaries import SummaryPolicy
from harborrag_core.topology.derived import DescriptionOutput
from harborrag_engine.topology.summary_reducer import SummaryBudgetDeferred
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.topology.summary_inputs import SummaryInputLoader
from harborrag_runtime.topology.summary_service import SummaryProjectionService


class Model:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def generate_usage(self, packets):
        self.calls.append(packets)
        if self.fail:
            raise RuntimeError("provider unavailable")
        return DescriptionRun(
            DescriptionOutput(
                description="Harbor service documentation.",
                topics=("Harbor",),
                cited_packet_ids=(packets[0].packet_id,),
                complete=True,
            ),
            HarborChatUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
            1,
            cost_usd=Decimal("0.001"),
        )


def service(harness, model):
    settings = RuntimeSettings(topology_llm_operation_cost_usd=Decimal("0.1"))
    loader = SummaryInputLoader(harness.control.summaries, harness.reader, harness.writer, settings)
    return SummaryProjectionService(
        harness.control.summaries,
        harness.control.topology,
        settings,
        lambda lease: model,
        loader.load,
    )


@pytest.mark.asyncio
async def test_published_content_summarizes_without_extraction_and_reuses_across_versions(tmp_path):
    async with Harness(tmp_path) as harness:
        harness.include_graph = True
        first = await harness.publish()
        await harness.control.summaries.configure(
            "DEFAULT", "scope", SummaryPolicy(model_fingerprint="model", debounce_seconds=0)
        )
        model = Model()
        runner = service(harness, model)
        assert await runner.run_once("DEFAULT") == "current"
        assert harness.model.calls == 0
        nodes = await harness.control.summaries.retained_nodes("DEFAULT", "scope")
        views = await harness.control.summaries.views(
            "DEFAULT", tuple(node.node_key for node in nodes), access=harness.access
        )
        assert views and all(view.status == "current" for view in views.values())
        call_count = len(model.calls)
        assert call_count > 0
        second = await harness.publish(revision="two")
        assert first.document_version_id != second.document_version_id
        assert await runner.run_once("DEFAULT") == "current"
        assert len(model.calls) == call_count
        nodes = await harness.control.summaries.retained_nodes("DEFAULT", "scope")
        current = [node for node in nodes if node.document_version_id == second.document_version_id]
        assert current
        views = await harness.control.summaries.views(
            "DEFAULT", tuple(node.node_key for node in current), access=harness.access
        )
        assert all(view.status == "current" for view in views.values())


@pytest.mark.asyncio
async def test_unconfigured_cost_defers_without_provider_calls(tmp_path):
    async with Harness(tmp_path) as harness:
        harness.include_graph = True
        await harness.publish()
        await harness.control.summaries.configure(
            "DEFAULT", "scope", SummaryPolicy(model_fingerprint="model", debounce_seconds=0)
        )
        model = Model()
        settings = RuntimeSettings()
        loader = SummaryInputLoader(
            harness.control.summaries, harness.reader, harness.writer, settings
        )
        runner = SummaryProjectionService(
            harness.control.summaries,
            harness.control.topology,
            settings,
            lambda lease: model,
            loader.load,
        )
        assert await runner.run_once("DEFAULT") == "blocked"
        assert not model.calls


@pytest.mark.asyncio
async def test_concurrent_summary_failures_prefer_infrastructure_error(tmp_path, caplog):
    async with Harness(tmp_path) as harness:
        await harness.publish()
        await harness.control.summaries.configure(
            "DEFAULT", "scope", SummaryPolicy(model_fingerprint="model", debounce_seconds=0)
        )

        async def fail_loading(_lease, _snapshot):
            raise ExceptionGroup(
                "concurrent summary failures",
                [SummaryBudgetDeferred("summary_call_budget"), RuntimeError("storage failed")],
            )

        runner = SummaryProjectionService(
            harness.control.summaries,
            harness.control.topology,
            RuntimeSettings(topology_llm_operation_cost_usd=Decimal("0.1")),
            lambda lease: Model(),
            fail_loading,
        )

        assert await runner.run_once("DEFAULT") == "failed"
        status = await harness.control.summaries.status("DEFAULT")
        assert status[0]["error_code"] == "RuntimeError"
        assert "Summary projection attempt failed" in caplog.text


@pytest.mark.asyncio
async def test_tenant_rollup_is_lazy_and_uses_authorized_source_cards(tmp_path):
    async with Harness(tmp_path) as harness:
        harness.include_graph = True
        await harness.publish()
        policy = SummaryPolicy(model_fingerprint="model", debounce_seconds=0, tenant_enabled=True)
        await harness.control.summaries.configure("DEFAULT", "scope", policy)
        await harness.control.summaries.configure("DEFAULT", "@tenant", policy)
        model = Model()
        runner = service(harness, model)
        assert await runner.run_once("DEFAULT") == "current"
        assert await runner.run_once("DEFAULT") == "idle"
        key = DocumentIdentityBuilder().tenant_node_key(tenant_id="DEFAULT")
        views = await harness.control.summaries.views(
            "DEFAULT", (key,), access=harness.access, source_scopes={key: "@tenant"}
        )
        assert views[key].status == "pending"
        assert await runner.run_once("DEFAULT") == "current"
        views = await harness.control.summaries.views("DEFAULT", (key,), access=harness.access)
        assert views[key].status == "current"
        assert views[key].child_count == 1


@pytest.mark.asyncio
async def test_publication_during_generation_cannot_publish_old_description(tmp_path):
    async with Harness(tmp_path) as harness:
        harness.include_graph = True
        original = await harness.publish()
        await harness.control.summaries.configure(
            "DEFAULT", "scope", SummaryPolicy(model_fingerprint="model", debounce_seconds=0)
        )

        class RacingModel(Model):
            async def generate_usage(self, packets):
                result = await super().generate_usage(packets)
                if len(self.calls) == 1:
                    await harness.publish(revision="replacement")
                return result

        assert await service(harness, RacingModel()).run_once("DEFAULT") == "blocked"
        assert not await harness.control.summaries.document_bindings(
            "DEFAULT", str(original.document_id), str(original.document_version_id)
        )
