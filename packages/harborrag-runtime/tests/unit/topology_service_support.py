"""Real canonical authority and immutable artifacts, with model/graph test doubles."""

from contextlib import asynccontextmanager
from datetime import timedelta

from harborrag_adapters.repositories.database.ingestion_control import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.database.sqlite.client import SQLiteDBClient
from harborrag_adapters.repositories.object_store import (
    ChunkArtifactReader,
    ChunkArtifactWriter,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    MemoryObjectStore,
)
from harborrag_adapters.topology.artifacts import ExtractionArtifacts
from harborrag_core.base import utc_now
from harborrag_core.chunking import (
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    ChangeFingerprintBuilder,
    DocumentIdentityBuilder,
    DocumentVersionCandidate,
    DocumentVersionState,
    ProcessingProfile,
    ProjectionManifest,
    SourceIdentity,
)
from harborrag_core.security.context import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import ExtractionOutput, ExtractionProfile, TopologyPolicy
from harborrag_core.topology.config import TenantIndexingConfig
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot
from harborrag_runtime.topology.service import TopologyEnrichmentService, TopologyResources


class FakeExtractor:
    def __init__(self):
        self.calls = 0
        self.inputs = []
        self.adaptive = False
        self.output = ExtractionOutput.model_validate(
            {
                "entities": [
                    {
                        "local_id": "a",
                        "name": "Harbor",
                        "entity_type": "service",
                        "span": {"start": 0, "end": 6, "quote": "Harbor"},
                    }
                ],
                "assertions": [],
            }
        )

    async def extract(self, value, **kwargs):
        self.calls += 1
        self.inputs.append(value)
        if self.adaptive:
            text = value.content[: min(20, len(value.content))]
            span = {"start": 0, "end": len(text), "quote": text}
            return ExtractionOutput.model_validate(
                {
                    "title": text,
                    "description": text,
                    "retrieval_context": text,
                    "title_evidence": [span],
                    "description_evidence": [span],
                    "retrieval_context_evidence": [span],
                    "entities": [],
                    "assertions": [],
                }
            )
        return self.output

    @asynccontextmanager
    async def factory(self, profile):
        yield self


class FakeProjection:
    def __init__(self):
        self.builds = {}
        self.valid = True
        self.before_write = None

    async def write(self, build, *, context):
        if self.before_write:
            await self.before_write()
        self.builds[build.build_id] = build

    async def verify(self, build, *, context):
        return self.valid and self.builds.get(build.build_id) == build

    async def delete_build(self, build_id, *, context):
        self.builds.pop(build_id, None)


class Harness:
    def __init__(self, tmp_path):
        self.control = IngestionControlPlaneDatabase(
            SQLiteDBClient(database=str(tmp_path / "topology.db")), create_schema=True
        )
        self.store = MemoryObjectStore()
        self.writer = ImmutableArtifactWriter(self.store)
        self.reader = ImmutableArtifactReader(self.store)
        self.artifacts = ExtractionArtifacts(self.writer, self.reader)
        self.model = FakeExtractor()
        self.access = AccessContext.system("DEFAULT")
        self.graph = FakeProjection()
        self.policy = TopologyPolicy(
            tenant_id="DEFAULT",
            source_scope_id="scope",
            enabled=True,
            profile=ExtractionProfile(model="test", deployment_revision="r1", prompt_digest="p1"),
        )
        self.resources = TopologyResources(
            self.control.topology,
            self.control.document_versions,
            ChunkArtifactReader(self.reader),
            self.artifacts,
            self.graph,
            self.model.factory,
        )
        self.service = TopologyEnrichmentService(self.resources)

    async def __aenter__(self):
        await self.control.connect()
        await self.store.connect()
        await self.control.topology.configure_indexing(
            TenantIndexingConfig(tenant_id="DEFAULT", enabled=True)
        )
        await self.control.topology.configure_policy(self.policy)
        return self

    async def __aexit__(self, *args):
        await self.store.close()
        await self.control.close()

    async def permit(self, document_id):
        now = utc_now()
        for kind, resource in (("source", "scope"), ("document", document_id)):
            await self.control.topology.set_permissions(
                ResolvedPermissionSnapshot(
                    tenant_id="DEFAULT",
                    resource_kind=kind,
                    resource_id=resource,
                    revision="acl-1",
                    resolved_at=now,
                    expires_at=now + timedelta(hours=1),
                    known=True,
                    processing_allowed=True,
                    public=True,
                )
            )

    async def publish(
        self,
        revision="one",
        *,
        item="page",
        include_route=False,
        content="Harbor is a service.",
        extra_content=(),
    ):
        source = SourceIdentity(
            tenant_id="DEFAULT",
            connector_type=ConnectorType.LOCAL,
            connection_id="files",
            source_item_id=item,
            source_scope_id="scope",
        )
        fingerprints = ChangeFingerprintBuilder().build(
            admission=AdmissionSnapshot(source_version=revision),
            canonical_evidence={"revision": revision, "content": content, "extra": extra_content},
            retrieval_metadata={"title": "Guide"},
            processing=ProcessingProfile(
                parser_profile="p1",
                normalizer_version="n1",
                chunk_strategy="c1",
                dense_encoder_profile="d1",
                sparse_encoder_profile="s1",
                graph_projection_version="g1",
            ),
        )
        identity = DocumentIdentityBuilder()
        document_id = identity.document_id(
            tenant_id="DEFAULT",
            connector_type=ConnectorType.LOCAL,
            connection_id="files",
            source_item_id=item,
        )
        await self.permit(str(document_id))
        version_id = identity.document_version_id(
            document_id=document_id,
            canonical_content_hash=fingerprints.canonical_content_hash,
            retrieval_metadata_hash=fingerprints.retrieval_metadata_hash,
            processing_fingerprint=fingerprints.processing_fingerprint,
        )
        candidate = DocumentVersionCandidate(
            document_id=document_id,
            document_version_id=version_id,
            source_identity=source,
            fingerprints=fingerprints,
        )
        chunk = ChunkRecord(
            strategy_version="c1",
            logical_chunk_id="logical-1",
            chunk_id=f"chunk-{version_id}",
            connector_type=ConnectorType.LOCAL,
            document_kind=DocumentKind.LOCAL_FILE,
            record_kind=RecordKind.EVIDENCE,
            chunk_kind=ChunkKind.TEXT,
            tenant_id="DEFAULT",
            connection_id="files",
            source_scope_id="scope",
            source_item_id=item,
            source_version=revision,
            document_id=document_id,
            document_version_id=version_id,
            ordinal=0,
            content=content,
            embedding_text=content,
            search_text=content,
            content_hash="content",
            token_count=5,
            security=ChunkSecurity(permission_set_id="permission-set:public"),
        )
        chunks = (chunk,)
        chunks += tuple(
            chunk.model_copy(
                update={
                    "chunk_id": f"chunk-{version_id}-{ordinal}",
                    "logical_chunk_id": f"logical-{ordinal + 1}",
                    "ordinal": ordinal,
                    "content": text,
                    "embedding_text": text,
                    "search_text": text,
                }
            )
            for ordinal, text in enumerate(extra_content, start=1)
        )
        if include_route:
            route = chunk.model_copy(
                update={
                    "record_kind": RecordKind.ROUTE,
                    "chunk_id": f"route-{version_id}",
                    "content": "Navigation metadata, not evidence.",
                }
            )
            chunks = (route, *chunks)
        artifacts = await ChunkArtifactWriter(self.writer).put(
            document_id=str(document_id),
            document_version_id=str(version_id),
            chunks=chunks,
            context=StorageOperationContext.system("DEFAULT"),
        )
        repo = self.control.document_versions
        await repo.create_candidate(candidate)
        for state in (
            DocumentVersionState.RAW_CAPTURED,
            DocumentVersionState.CANONICAL_READY,
            DocumentVersionState.CHUNKS_READY,
            DocumentVersionState.REPRESENTATIONS_READY,
            DocumentVersionState.PROJECTIONS_STAGED,
        ):
            if state == DocumentVersionState.CHUNKS_READY:
                await repo.transition(
                    str(version_id),
                    state,
                    artifact_column="chunk_artifact",
                    artifact=artifacts.chunks,
                )
            else:
                await repo.transition(str(version_id), state)
        await repo.save_projection_manifest(
            ProjectionManifest(
                document_id=document_id,
                document_version_id=version_id,
                evidence_point_ids=tuple(
                    str(row.chunk_id) for row in chunks if row.record_kind == RecordKind.EVIDENCE
                ),
                chunk_ids=tuple(
                    str(row.chunk_id) for row in chunks if row.record_kind == RecordKind.EVIDENCE
                ),
            )
        )
        await repo.mark_verified(str(version_id))
        await self.control.publisher.publish(
            document_id=str(document_id), candidate_document_version_id=str(version_id)
        )
        return candidate
