"""Small canonical and vector collaborators for semantic retrieval tests."""

from __future__ import annotations

from dataclasses import replace

from retrieval_test_support import FakeVectorRepository, policy, resources

from harborrag_core.indexing import VectorIndexRecord, VectorSearchResult
from harborrag_core.ingestion import ActiveDocumentVersion, DocumentIdentityBuilder
from harborrag_core.topology.extraction import EvidenceSpan, ExtractedAssertion, ExtractedEntity
from harborrag_core.topology.records import CanonicalAssertion, CanonicalMention
from harborrag_runtime.retrieval import RuntimeRetrievalService


def mention(entity, chunk, name, *, owner=("build-1", "document-1", "version-1")):
    build, document, version = owner
    return CanonicalMention(
        mention_id=f"mention-{entity}-{chunk}",
        entity_id=entity,
        tenant_id="tenant-1",
        build_id=build,
        document_id=document,
        document_version_id=version,
        chunk_id=chunk,
        observation=ExtractedEntity(
            local_id=entity,
            name=name,
            entity_type="service",
            span=EvidenceSpan(start=0, end=len(name), quote=name),
        ),
    )


class CanonicalTopology:
    def __init__(self):
        self.mentions = (
            mention("entity-1", "chunk-1", "Alpha"),
            mention(
                "entity-2",
                "chunk-2",
                "Beta",
                owner=("build-2", "document-2", "version-2"),
            ),
        )
        self.assertions = (
            CanonicalAssertion(
                assertion_id="assertion-1",
                tenant_id="tenant-1",
                build_id="build-1",
                document_id="document-1",
                document_version_id="version-1",
                chunk_id="chunk-1",
                subject_entity_id="entity-1",
                object_entity_id="entity-2",
                observation=ExtractedAssertion(
                    local_id="a1",
                    subject_id="entity-1",
                    object_id="entity-2",
                    predicate="depends_on",
                    span=EvidenceSpan(start=0, end=22, quote="Alpha depends on Beta."),
                ),
            ),
        )
        self.eligible = {"build-1", "build-2"}
        self.calls = []
        self.validation_calls = []

    async def active_mentions(  # noqa: PLR0913 - mirrors the authorized repository contract
        self, tenant_id, *, labels=(), chunk_ids=(), entity_ids=(), limit=100, access=None
    ):
        self.calls.append((tenant_id, labels, chunk_ids, entity_ids, limit))
        labels = {label.casefold() for label in labels}
        return tuple(
            item
            for item in self.mentions
            if item.tenant_id == tenant_id
            and (
                item.observation.name.casefold() in labels
                or item.chunk_id in chunk_ids
                or item.entity_id in entity_ids
            )
        )[:limit]

    async def active_assertions(self, tenant_id, *, entity_ids=(), limit=100, access=None):
        return tuple(
            item
            for item in self.assertions
            if item.tenant_id == tenant_id
            and (item.subject_entity_id in entity_ids or item.object_entity_id in entity_ids)
        )[:limit]

    async def eligible_build_ids(self, tenant_id, build_ids, *, access=None):
        self.validation_calls.append((tenant_id, build_ids))
        return set(build_ids) & self.eligible

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        return ("document-1", "document-2")

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        return set(document_ids) & set(await self.allowed_document_ids(tenant_id, access=access))


class TopologyVectors(FakeVectorRepository):
    def __init__(self):
        super().__init__()
        self.get_calls = []
        self.records = [
            record("chunk-1", "document-1", "version-1"),
            record("chunk-2", "document-2", "version-2"),
        ]

    def _results(self, collection):
        item = self.records[0]
        return [VectorSearchResult(id=item.id, score=0.9, raw_score=0.9, payload=item.payload)]

    async def get_records(self, collection, ids, *, context):
        self.get_calls.append((collection, ids, context))
        return [item for item in self.records if item.id in ids]


def record(chunk, document, version):
    return VectorIndexRecord(
        id=DocumentIdentityBuilder().point_id(chunk_id=chunk),
        tenant_id="tenant-1",
        vector=[1, 0, 0],
        payload={
            "chunk_id": chunk,
            "document_id": document,
            "document_version_id": version,
            "record_kind": "evidence",
            "chunk_kind": "text",
            "connector_type": "local",
            "content": "Alpha depends on Beta." if chunk == "chunk-1" else "Beta controls retries.",
        },
    )


class ActiveVersions:
    def __init__(self):
        self.versions = {"document-1": "version-1", "document-2": "version-2"}

    async def active_versions(self, document_ids):
        return {
            document: ActiveDocumentVersion(
                document_id=document, document_version_id=self.versions[document]
            )
            for document in document_ids
            if document in self.versions
        }


def service(topology, vectors=None, authority=None):
    return RuntimeRetrievalService(
        resources=replace(
            resources(
                vectors=vectors or TopologyVectors(), active_versions=authority or ActiveVersions()
            ),
            topology_repository=topology,
        ),
        policy=policy(),
    )
