"""An entity's evidence is released only from the datasources the reader is entitled to."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.base import utc_now
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import GraphNodeRecord, SourceIdentity
from harborrag_core.ingestion.states import (
    GraphEntityType,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.security import AccessContext
from harborrag_core.summaries import SummaryBinding, SummaryCard, SummaryManifest, SummaryPolicy

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    publish_over_active,
    source_identity,
)
from .topology_fixtures import permit

ENGINEERING = "scope-engineering"
HR = "scope-hr"


def _identity(scope: str) -> SourceIdentity:
    if scope == ENGINEERING:
        return source_identity("candidate-eng")
    return SourceIdentity(
        tenant_id="DEFAULT",
        connector_type=ConnectorType.CONFLUENCE,
        connection_id="wiki.hr",
        source_item_id="candidate-hr",
        source_scope_id=scope,
    )


async def release_entity(control: IngestionControlPlaneDatabase, scope: str) -> tuple[str, str]:
    """Publish one document in ``scope`` and accept its entity card; return its key and chunk."""

    version = candidate(f"content of {scope}", source=_identity(scope))
    await permit(control, str(version.document_id), scope)
    await advance_to_verified(control, version)
    await publish_over_active(control, str(version.document_id), str(version.document_version_id))
    await control.summaries.configure(
        "DEFAULT", scope, SummaryPolicy(model_fingerprint="model-v1", debounce_seconds=0)
    )
    lease = await control.summaries.claim("DEFAULT", source_scope_id=scope)
    assert lease is not None
    snapshot = await control.summaries.snapshot(lease)
    node_key, chunk = f"entity:{scope}", f"chunk-{scope}"
    card = SummaryCard(description=f"The candidate of {scope}.", topics=("hiring",))
    binding = SummaryBinding(
        manifest=SummaryManifest(
            node_key=node_key,
            kind="SourceEntity",
            source_scope_id=scope,
            input_document_versions=snapshot.document_versions,
            permission_dependencies=snapshot.permission_dependencies,
            policy_fingerprint=lease.policy.fingerprint,
            membership_digest=snapshot.membership_digest,
            input_digest="input",
            input_chunk_ids=(chunk,),
        ),
        card=card,
        generation_key="generation",
        artifact_hash=card.artifact_hash,
        revision=lease.revision,
        updated_at=utc_now(),
    )
    node = GraphNodeRecord(
        node_key=node_key,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.CONFLUENCE_PAGE,
        logical_id=node_key,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="DEFAULT",
        source_scope_id=scope,
        title="Candidate",
    )
    await control.summaries.accept(lease, snapshot, binding, node)
    return node_key, chunk


def reader(entitled: frozenset[str] | None) -> AccessContext:
    return AccessContext(
        principal_id="reader", tenant_id="DEFAULT", entitled_source_scopes=entitled
    )


@pytest.mark.asyncio
async def test_entity_evidence_drops_chunks_of_other_datasources(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        eng_key, eng_chunk = await release_entity(control, ENGINEERING)
        hr_key, hr_chunk = await release_entity(control, HR)
        keys = (eng_key, hr_key)

        async def released(entitled: frozenset[str] | None) -> dict[str, tuple[str, ...]]:
            return await control.summaries.entity_evidence("DEFAULT", keys, access=reader(entitled))

        # No entitlement set: the gate is off and both datasources serve.
        assert await released(None) == {eng_key: (eng_chunk,), hr_key: (hr_chunk,)}
        assert await released(frozenset({ENGINEERING, HR})) == {
            eng_key: (eng_chunk,),
            hr_key: (hr_chunk,),
        }
        assert await released(frozenset({ENGINEERING})) == {eng_key: (eng_chunk,)}
        assert await released(frozenset({HR})) == {hr_key: (hr_chunk,)}
        # An entitlement to some other datasource releases nothing, and so does none at all.
        assert await released(frozenset({"scope-finance"})) == {}
        assert await released(frozenset()) == {}
