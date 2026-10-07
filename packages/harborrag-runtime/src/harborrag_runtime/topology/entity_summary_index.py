"""Make an accepted source-entity card reachable by search, not only by navigation.

A source entity -- a Jira issue with its comments and its attachments -- is the
first level of the aggregation forest that spans documents, and it is the level a
question like "who cleared the second interview round" is actually about. Chunk
search cannot answer that, because the answer is not in any one chunk.

The point written here carries no card text and no chunk identities: it is a
routing key. Everything servable is re-read through ``SummaryReaderPort``, which
re-checks the binding's permission dependencies and freshness on every request.
That is why this product stays out of ``DERIVED_VECTOR_PRODUCTS`` -- those points
are served under one build's identity, and an entity summary composes documents
whose permissions are not one build's to assert.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from harborrag_core.indexing import VectorIndexRecord
from harborrag_core.models.embed import (
    EmbeddingPurpose,
    HarborEmbedMetadata,
    HarborEmbedRequest,
)
from harborrag_core.ports.storage import VectorRepositoryPort
from harborrag_core.schemas.ids import TenantId
from harborrag_core.schemas.vector import (
    VectorDistance,
    VectorFilter,
    VectorFilterCondition,
    VectorIndexSpec,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_core.summaries import SummaryBinding, SummaryFacet
from harborrag_core.topology.derived import (
    ENTITY_SUMMARY_RECORD_KIND,
    ContextualIndexProfile,
)
from harborrag_engine.topology.vector_values import canonical_dense_vector

from .contextual import Embedder

_MAX_SCAN_PAGE = 512


FACET_PAYLOAD_ROOT = "facet"


def facet_payload_key(name: str) -> str:
    """The filter field for one facet: what a caller writes and Qdrant indexes."""

    return f"{FACET_PAYLOAD_ROOT}.{name}"


def facet_payload(
    binding: SummaryBinding, facets: tuple[SummaryFacet, ...]
) -> dict[str, list[str] | list[int]]:
    """Flatten the card's attributes into filterable values, typed per facet.

    An integer facet is stored as numbers so a range filter (``years >= 5``)
    works; a value that does not parse is dropped from that facet rather than
    stored as text under a numeric field, where it would match nothing.
    """

    kinds = {facet.name: facet.kind for facet in facets}
    payload: dict[str, list[str] | list[int]] = {}
    for item in binding.card.attributes:
        if item.name not in kinds:
            continue
        if kinds[item.name] == "integer":
            numbers = [number for value in item.values if (number := _integer(value)) is not None]
            if numbers:
                payload[item.name] = numbers
            continue
        payload[item.name] = [value.strip().casefold() for value in item.values if value.strip()]
    return payload


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _integer(value: str) -> int | None:
    """The first number in the text, truncated: ``"200.0"`` is 200, ``"5+ years"`` is 5."""

    match = _NUMBER.search(value)
    if match is None:
        return None
    try:
        return int(float(match.group()))
    except (ValueError, OverflowError):
        return None


def entity_point_id(tenant_id: str, node_key: str, fingerprint: str) -> str:
    """One stable point per entity and profile, so a regeneration overwrites."""

    return str(uuid5(NAMESPACE_URL, f"{tenant_id}:entity:{node_key}:{fingerprint}"))


def entity_embedding_text(binding: SummaryBinding) -> str:
    """Embed the dossier with the facets that make it filterable.

    The facets are repeated into the embedded text on purpose: a query naming a
    skill or a pipeline stage should reach the entity through the dense lane even
    when the prose never spells that facet out as a sentence.
    """

    card = binding.card
    facets = [
        *card.topics,
        *card.key_entities,
        *(f"{item.name}: {', '.join(item.values)}" for item in card.attributes),
    ]
    return "\n".join([card.description, *facets]) if facets else card.description


@dataclass(frozen=True)
class EntitySummaryIndex:
    """Publish and prune the source-entity summary points for one source scope."""

    vectors: VectorRepositoryPort
    embed: Embedder
    profile: ContextualIndexProfile

    async def publish(
        self,
        tenant_id: str,
        source_scope_id: str,
        bindings: tuple[SummaryBinding, ...],
        facets: tuple[SummaryFacet, ...] = (),
    ) -> int:
        """Write one point per entity and remove the ones the scope no longer has.

        Pruning is part of publishing rather than a separate cleanup pass: these
        points have no build to retire them, so the run that owns the scope is the
        only place that knows the live set. ``facets`` name the payload keys that
        get a payload index, so a filter on them is a lookup rather than a scan.
        """

        context = StorageOperationContext.system(tenant_id)
        index = self.profile.entity_index_name
        records = [
            await self._record(tenant_id, source_scope_id, item, facets) for item in bindings
        ]
        if records:
            await self._ensure_index(context, facets)
            await self.vectors.upsert_records(index, records, context=context)
        elif not await self.vectors.index_exists(index, context=context):
            return 0
        await self._prune(tenant_id, source_scope_id, {row.id for row in records}, context)
        return len(records)

    async def _ensure_index(
        self, context: StorageOperationContext, facets: tuple[SummaryFacet, ...]
    ) -> None:
        await self.vectors.ensure_index(
            VectorIndexSpec(
                index_name=self.profile.entity_index_name,
                dimension=self.profile.dimension,
                distance=VectorDistance.DOT_PRODUCT,
                metadata_indexes=[
                    "tenant_id",
                    "projection_point_id",
                    "node_key",
                    "source_scope_id",
                    "embedding_profile",
                    "coverage_mode",
                    *(facet_payload_key(facet.name) for facet in facets),
                ],
            ),
            context=context,
        )

    async def _prune(
        self,
        tenant_id: str,
        source_scope_id: str,
        live: set[str],
        context: StorageOperationContext,
    ) -> None:
        filters = VectorFilter(
            must=[VectorFilterCondition(field="source_scope_id", value=source_scope_id)]
        )
        cursor: str | None = None
        while True:
            page = await self.vectors.scan_records(
                self.profile.entity_index_name,
                limit=_MAX_SCAN_PAGE,
                cursor=cursor,
                filters=filters,
                context=context,
            )
            stale = tuple(
                row.id
                for row in page.records
                if row.id not in live and str(row.tenant_id) == tenant_id
            )
            if stale:
                await self.vectors.delete_records(
                    self.profile.entity_index_name, stale, context=context
                )
            cursor = page.next_cursor
            if cursor is None:
                return

    async def _record(
        self,
        tenant_id: str,
        source_scope_id: str,
        binding: SummaryBinding,
        facets: tuple[SummaryFacet, ...],
    ) -> VectorIndexRecord:
        text = entity_embedding_text(binding)
        if len(text.encode()) > self.profile.max_input_bytes:
            raise ValueError("entity summary exceeds embedding input budget")
        response = await self.embed.aembed(
            HarborEmbedRequest(
                inputs=(text,),
                logical_model=self.profile.model,
                dimensions=self.profile.dimension,
                purpose=EmbeddingPurpose.DOCUMENT,
                cacheable=False,
                normalize=True,
                sensitive=True,
                metadata=HarborEmbedMetadata(
                    tenant_id=tenant_id,
                    document_ids=tuple(binding.manifest.input_document_versions),
                ),
            )
        )
        if len(response.embeddings) != 1 or not isinstance(response.embeddings[0].value, tuple):
            raise ValueError("entity summary embedding is not a single dense vector")
        vector = canonical_dense_vector(response.embeddings[0].value)
        if len(vector) != self.profile.dimension:
            raise ValueError("entity summary embedding dimension mismatch")
        identity = entity_point_id(
            tenant_id, binding.manifest.node_key, self.profile.entity_fingerprint
        )
        return VectorIndexRecord(
            id=identity,
            tenant_id=TenantId(tenant_id),
            vector=list(vector),
            payload={
                "record_kind": ENTITY_SUMMARY_RECORD_KIND,
                "projection_point_id": identity,
                "node_key": binding.manifest.node_key,
                "source_scope_id": source_scope_id,
                "embedding_profile": self.profile.entity_fingerprint,
                "artifact_hash": binding.artifact_hash,
                "coverage_mode": binding.coverage_mode,
                # Facet names and values only: the dossier text itself is never
                # stored here, because only the summary authority may serve it.
                "attributes": json.loads(
                    json.dumps(
                        [
                            {"name": item.name, "values": list(item.values)}
                            for item in binding.card.attributes
                        ]
                    )
                ),
                "attribute_names": sorted(item.name for item in binding.card.attributes),
                # Nested under one key so a filter written as ``facet.stage`` --
                # the dotted path Qdrant resolves -- lands on the indexed field.
                FACET_PAYLOAD_ROOT: facet_payload(binding, facets),
            },
        )
