"""A card is released only for the datasources the reader is entitled to.

``views`` is the one read both callers go through: the graph-node cards
(``apply_summary_views``, which also passes the nodes' source scopes) and the entity cards
``find_entities`` returns (which passes none). The rule lives there so neither can bypass it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.base import utc_now
from harborrag_core.ingestion import DocumentIdentityBuilder, GraphNodeRecord
from harborrag_core.ingestion.states import (
    GraphEntityType,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.summaries import SummaryBinding, SummaryCard, SummaryManifest, SummaryPolicy

from .ingestion_control_fixtures import make_control_plane
from .test_entity_evidence_entitlements import ENGINEERING, HR, reader, release_entity

TENANT_SCOPE = "@tenant"


async def release_tenant_card(control: IngestionControlPlaneDatabase) -> str:
    """Accept the tenant-wide rollup card over every datasource published so far."""

    policy = SummaryPolicy(model_fingerprint="model-v1", debounce_seconds=0, tenant_enabled=True)
    await control.summaries.configure("DEFAULT", TENANT_SCOPE, policy)
    # The rollup is queued by the worker's reconcile, never by a read.
    assert await control.summaries.reconcile("DEFAULT") >= 1
    lease = await control.summaries.claim("DEFAULT", source_scope_id=TENANT_SCOPE)
    assert lease is not None
    snapshot = await control.summaries.snapshot(lease)
    key = DocumentIdentityBuilder().tenant_node_key(tenant_id="DEFAULT")
    card = SummaryCard(description="Everything the tenant holds.", topics=("all",))
    binding = SummaryBinding(
        manifest=SummaryManifest(
            node_key=key,
            kind="Tenant",
            source_scope_id=TENANT_SCOPE,
            input_document_versions=snapshot.document_versions,
            permission_dependencies=snapshot.permission_dependencies,
            policy_fingerprint=lease.policy.fingerprint,
            membership_digest=snapshot.membership_digest,
            input_digest="input",
            input_chunk_ids=("tenant-chunk",),
        ),
        card=card,
        generation_key="generation",
        artifact_hash=card.artifact_hash,
        revision=lease.revision,
        updated_at=utc_now(),
    )
    node = GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.TENANT,
        entity_type=GraphEntityType.TENANT,
        logical_id="DEFAULT",
        ownership_scope=GraphOwnershipScope.TENANT,
        owner_id="DEFAULT",
        title="DEFAULT",
    )
    await control.summaries.accept(lease, snapshot, binding, node)
    return key


@pytest.mark.asyncio
async def test_entity_cards_only_for_entitled_datasources(tmp_path: Path) -> None:
    """The call ``find_entities`` makes: keys only, no source scopes."""

    async with make_control_plane(tmp_path) as control:
        eng_key, _ = await release_entity(control, ENGINEERING)
        hr_key, _ = await release_entity(control, HR)
        keys = (eng_key, hr_key)

        async def cards(entitled: frozenset[str] | None) -> set[str]:
            views = await control.summaries.views("DEFAULT", keys, access=reader(entitled))
            return {key for key, view in views.items() if view.card is not None}

        assert await cards(None) == {eng_key, hr_key}
        assert await cards(frozenset({ENGINEERING, HR})) == {eng_key, hr_key}
        assert await cards(frozenset({ENGINEERING})) == {eng_key}
        assert await cards(frozenset({HR})) == {hr_key}
        assert await cards(frozenset({"scope-finance"})) == set()
        assert await cards(frozenset()) == set()


@pytest.mark.asyncio
async def test_scope_cards_only_for_entitled_datasources(tmp_path: Path) -> None:
    """The call ``apply_summary_views`` makes: the nodes' source scopes ride along.

    A card that is withheld must also not come back as a ``pending`` one, which would say a
    summary of that datasource is on its way.
    """

    async with make_control_plane(tmp_path) as control:
        eng_key, _ = await release_entity(control, ENGINEERING)
        hr_key, _ = await release_entity(control, HR)
        scopes = {eng_key: ENGINEERING, hr_key: HR}

        views = await control.summaries.views(
            "DEFAULT",
            (eng_key, hr_key),
            access=reader(frozenset({ENGINEERING})),
            source_scopes=scopes,
        )

        assert set(views) == {eng_key}
        assert views[eng_key].card is not None


@pytest.mark.asyncio
async def test_tenant_card_hidden_for_a_narrowed_reader(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await release_entity(control, ENGINEERING)
        await release_entity(control, HR)
        tenant_key = await release_tenant_card(control)
        scopes = {tenant_key: TENANT_SCOPE}

        ungated = await control.summaries.views(
            "DEFAULT", (tenant_key,), access=reader(None), source_scopes=scopes
        )
        everything = await control.summaries.views(
            "DEFAULT",
            (tenant_key,),
            access=reader(frozenset({ENGINEERING, HR})),
            source_scopes=scopes,
        )
        narrowed = await control.summaries.views(
            "DEFAULT", (tenant_key,), access=reader(frozenset({ENGINEERING})), source_scopes=scopes
        )
        keys_only = await control.summaries.views(
            "DEFAULT", (tenant_key,), access=reader(frozenset({ENGINEERING}))
        )

        # With no entitlement set the card is there, so the absence below is the rule.
        assert ungated[tenant_key].card is not None
        # A reader entitled to every datasource still has no say over the tenant-wide card:
        # the rule is "narrowed reader", and a set is narrowed whatever it holds.
        assert everything == {}
        # Neither a card nor a ``pending`` placeholder: the tenant node keeps its name only.
        assert narrowed == {}
        assert keys_only == {}
