"""A card says whether it was written over everything the source actually has."""

from decimal import Decimal

import pytest
from topology_service_support import Harness

from harborrag_adapters.topology.descriptions import DescriptionRun
from harborrag_core.base import utc_now
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import (
    BindingKind,
    DiscoveredSourceItem,
    DocumentIdentityBuilder,
    SourceBinding,
    SourceIdentity,
)
from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.summaries import MissingSourceDocument, SummaryPolicy
from harborrag_core.topology.derived import DescriptionOutput
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.topology.summary_inputs import SummaryInputLoader
from harborrag_runtime.topology.summary_service import (
    SummaryProjectionService,
    _coverage_mode,
    _CoverageLedger,
)


class Model:
    async def generate_usage(self, packets, **_):
        return DescriptionRun(
            DescriptionOutput(
                description="Harbor service documentation.",
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


def document_id(item: str) -> str:
    return str(
        DocumentIdentityBuilder().document_id(
            tenant_id="DEFAULT",
            connector_type=ConnectorType.LOCAL,
            connection_id="files",
            source_item_id=item,
        )
    )


async def discover(harness, *, item: str, parent: str | None = None) -> str:
    """Record an item the connector saw, whether or not it ever published."""

    await harness.control.source_scans.register_scope(
        tenant_id="DEFAULT",
        source_scope_id="scope",
        connector_type="local",
        connection_id="files",
        configuration_fingerprint="config-1",
    )
    scan_id = await harness.control.source_scans.start("scope")
    identity = SourceIdentity(
        tenant_id="DEFAULT",
        connector_type=ConnectorType.LOCAL,
        connection_id="files",
        source_item_id=item,
        source_scope_id="scope",
        binding=SourceBinding(kind=BindingKind.ROOT)
        if parent is None
        else SourceBinding(kind=BindingKind.ATTACHMENT, parent_source_item_id=parent),
    )
    await harness.control.source_scans.record_seen(
        scan_id=scan_id,
        item=DiscoveredSourceItem(
            source_identity=identity,
            document_id=document_id(item),
            source_version="one",
            admission_change_key=f"key-{item}",
        ),
    )
    await harness.control.source_scans.complete(scan_id)
    return document_id(item)


@pytest.mark.asyncio
async def test_an_attachment_that_never_published_makes_its_entity_partial(tmp_path):
    async with Harness(tmp_path) as harness:
        harness.include_graph = True
        await harness.publish(item="page")
        await discover(harness, item="page")
        attachment = await discover(harness, item="page-cv.pdf", parent="page")

        missing = await harness.control.summaries.missing_documents("DEFAULT", "scope")
        assert missing == (
            MissingSourceDocument(document_id=attachment, parent_document_id=document_id("page")),
        )

        await harness.control.summaries.configure(
            "DEFAULT", "scope", SummaryPolicy(model_fingerprint="model", debounce_seconds=0)
        )
        assert await service(harness, Model()).run_once("DEFAULT") == "current"
        nodes = await harness.control.summaries.retained_nodes("DEFAULT", "scope")
        views = await harness.control.summaries.views(
            "DEFAULT", tuple(node.node_key for node in nodes), access=harness.access
        )
        modes = {view.coverage_mode for view in views.values()}
        assert "partial" in modes
        partial = [view for view in views.values() if view.coverage_mode == "partial"]
        assert all(view.missing_documents == 1 for view in partial)


@pytest.mark.asyncio
async def test_a_scope_whose_every_item_published_stays_complete(tmp_path):
    async with Harness(tmp_path) as harness:
        harness.include_graph = True
        await harness.publish(item="page")
        await discover(harness, item="page")
        assert await harness.control.summaries.missing_documents("DEFAULT", "scope") == ()
        await harness.control.summaries.configure(
            "DEFAULT", "scope", SummaryPolicy(model_fingerprint="model", debounce_seconds=0)
        )
        assert await service(harness, Model()).run_once("DEFAULT") == "current"
        nodes = await harness.control.summaries.retained_nodes("DEFAULT", "scope")
        views = await harness.control.summaries.views(
            "DEFAULT", tuple(node.node_key for node in nodes), access=harness.access
        )
        assert {view.coverage_mode for view in views.values()} == {"complete"}
        assert all(view.missing_documents is None for view in views.values())


def test_coverage_is_the_three_states_a_card_can_honestly_claim():
    assert _coverage_mode(("chunk-1",), ()) == "complete"
    assert _coverage_mode((), ()) == "empty"
    assert _coverage_mode(("chunk-1",), ("doc-2",)) == "partial"
    # Nothing arrived and something is known to be missing: still partial, because
    # "empty" would read as "this entity has no content" rather than "not yet".
    assert _coverage_mode((), ("doc-2",)) == "partial"


def test_a_missing_child_is_attributed_to_the_entity_that_should_contain_it():
    ledger = _CoverageLedger.of(
        (
            MissingSourceDocument(document_id="cv", parent_document_id="issue"),
            MissingSourceDocument(document_id="orphan"),
            MissingSourceDocument(document_id="grandchild", parent_document_id="orphan"),
        )
    )
    assert ledger.missing_for("SourceEntity", ("issue",)) == ("cv",)
    assert ledger.missing_for("SourceEntity", ("unrelated",)) == ()
    # A child of an absent parent has no entity node to belong to, so it rises to
    # the source rather than vanishing from the count.
    assert ledger.missing_for("DataSource", ("issue",)) == ("cv", "grandchild", "orphan")


@pytest.mark.asyncio
async def test_entity_cards_wait_for_ingestion_to_settle_but_leaves_do_not(tmp_path):
    """While documents are still arriving an entity card would only be superseded by
    the next publish, so the run builds the per-version leaves, defers the entity
    level on the scope's settle window, and finishes the tree once ingestion ends."""

    from sqlalchemy import update

    from harborrag_adapters.repositories.database.ingestion_control.schema import INGESTION_TASKS
    from harborrag_core.ingestion import IngestionTask, IngestionTaskState

    async with Harness(tmp_path) as harness:
        harness.include_graph = True
        await harness.publish()
        await harness.control.summaries.configure(
            "DEFAULT",
            "scope",
            SummaryPolicy(model_fingerprint="model", debounce_seconds=0, max_wait_seconds=90),
        )
        await harness.control.tasks.create(
            IngestionTask(
                task_id="task-1",
                source_scope_id="scope",
                status=IngestionTaskState.PENDING,
                request={"tenant_id": "DEFAULT", "connector": "local"},
            )
        )
        runner = service(harness, Model())
        assert await runner.run_once("DEFAULT") == "blocked"
        kinds = {
            binding.manifest.kind
            for _, binding in await harness.control.summaries.document_bindings(
                "DEFAULT", *await _active_document(harness)
            )
        }
        assert kinds and kinds <= {"Structure", "DocumentVersion"}
        (scope,) = [
            row
            for row in await harness.control.summaries.status("DEFAULT")
            if row["source_scope_id"] == "scope"
        ]
        assert scope["error_code"] == "SUMMARY_INGESTION_ACTIVE"
        assert scope["execution"] == "queued"
        assert 60 <= (scope["available_at"] - utc_now()).total_seconds() <= 91

        async with harness.control.tasks._client.sessions.begin() as session:
            await session.execute(
                update(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == "task-1")
                .values(status=IngestionTaskState.COMPLETED.value)
            )
        await harness.control.summaries.backfill("DEFAULT", "scope")
        assert await runner.run_once("DEFAULT") == "current"
        nodes = await harness.control.summaries.retained_nodes("DEFAULT", "scope")
        assert any(node.node_kind == "SourceEntity" for node in nodes)


async def _active_document(harness) -> tuple[str, str]:
    rows = await harness.control.summaries.source_documents("DEFAULT", "scope")
    return str(rows[0]["document_id"]), str(rows[0]["active_document_version_id"])
