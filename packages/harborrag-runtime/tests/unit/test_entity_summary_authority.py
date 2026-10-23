"""End to end: an accepted entity card becomes searchable, and stays permissioned."""

from datetime import timedelta
from decimal import Decimal

import pytest
from test_entity_summary_search import PROFILE, Embed, Vectors
from topology_service_support import Harness

from harborrag_adapters.topology.descriptions import DescriptionRun
from harborrag_core.base import utc_now
from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.security.context import AccessContext
from harborrag_core.summaries import SummaryPolicy
from harborrag_core.topology.derived import DescriptionOutput
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.topology.entity_summary_index import EntitySummaryIndex
from harborrag_runtime.topology.summary_inputs import SummaryInputLoader
from harborrag_runtime.topology.summary_service import SummaryProjectionService


def private_permits(harness, allowed: tuple[str, ...]):
    """Resolve this scope's ACL as private to a named principal set, from the start.

    A snapshot cannot be narrowed under the same revision -- changing what a
    revision means is exactly what the authority refuses -- so the restriction has
    to be in place before the card that depends on it is written.
    """

    async def permit(document_id: str) -> None:
        now = utc_now()
        for kind, resource in (("source", "scope"), ("document", document_id)):
            await harness.control.topology.set_permissions(
                ResolvedPermissionSnapshot(
                    tenant_id="DEFAULT",
                    resource_kind=kind,
                    resource_id=resource,
                    revision="acl-1",
                    resolved_at=now,
                    expires_at=now + timedelta(hours=1),
                    known=True,
                    processing_allowed=True,
                    public=False,
                    allowed_principal_ids=allowed,
                )
            )

    harness.permit = permit


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


def service(harness, index):
    settings = RuntimeSettings(topology_llm_operation_cost_usd=Decimal("0.1"))
    loader = SummaryInputLoader(harness.control.summaries, harness.reader, harness.writer, settings)
    return SummaryProjectionService(
        harness.control.summaries,
        harness.control.topology,
        settings,
        lambda lease: Model(),
        loader.load,
        index,
    )


async def project(harness, index):
    harness.include_graph = True
    await harness.publish()
    await harness.control.summaries.configure(
        "DEFAULT", "scope", SummaryPolicy(model_fingerprint="model", debounce_seconds=0)
    )
    assert await service(harness, index).run_once("DEFAULT") == "current"


@pytest.mark.asyncio
async def test_source_entity_cards_reach_the_index_and_nothing_else_does(tmp_path):
    async with Harness(tmp_path) as harness:
        vectors = Vectors()
        await project(harness, EntitySummaryIndex(vectors, Embed(), PROFILE))
        points = vectors.records[PROFILE.entity_index_name].values()
        assert points, "the scope's source entity should be searchable"
        nodes = await harness.control.summaries.retained_nodes("DEFAULT", "scope")
        by_key = {node.node_key: node for node in nodes}
        # Sections and document versions stay read-time only: they are navigation,
        # and they already have a searchable parent-description product.
        assert {by_key[str(row.payload["node_key"])].node_kind for row in points} == {
            "SourceEntity"
        }


@pytest.mark.asyncio
async def test_the_authority_releases_evidence_only_to_a_principal_that_may_read_it(tmp_path):
    async with Harness(tmp_path) as harness:
        private_permits(harness, ("harborrag-runtime",))
        vectors = Vectors()
        await project(harness, EntitySummaryIndex(vectors, Embed(), PROFILE))
        keys = tuple(
            str(row.payload["node_key"])
            for row in vectors.records[PROFILE.entity_index_name].values()
        )
        released = await harness.control.summaries.entity_evidence(
            "DEFAULT", keys, access=harness.access
        )
        assert released and all(chunks for chunks in released.values())

        # The same point, asked for by a principal the snapshot does not allow.
        stranger = AccessContext(
            tenant_id="DEFAULT", principal_id="stranger", corpus_mode="source_acl"
        )
        assert (
            await harness.control.summaries.entity_evidence("DEFAULT", keys, access=stranger) == {}
        )
        # And a different tenant asking for this tenant's keys gets nothing.
        assert (
            await harness.control.summaries.entity_evidence(
                "OTHER", keys, access=AccessContext.system("OTHER")
            )
            == {}
        )


@pytest.mark.asyncio
async def test_a_republished_document_makes_the_old_card_stop_serving(tmp_path):
    async with Harness(tmp_path) as harness:
        vectors = Vectors()
        await project(harness, EntitySummaryIndex(vectors, Embed(), PROFILE))
        keys = tuple(
            str(row.payload["node_key"])
            for row in vectors.records[PROFILE.entity_index_name].values()
        )
        assert await harness.control.summaries.entity_evidence(
            "DEFAULT", keys, access=harness.access
        )
        # The point is still in the index; the binding behind it is now stale, and
        # staleness is decided per request rather than by deleting the point.
        await harness.publish(revision="two")
        assert (
            await harness.control.summaries.entity_evidence("DEFAULT", keys, access=harness.access)
            == {}
        )
