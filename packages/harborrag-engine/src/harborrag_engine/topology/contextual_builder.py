"""Pure builders for contextual projections; source evidence is never modified."""

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from harborrag_core.indexing import VectorIndexRecord
from harborrag_core.schemas.ids import TenantId
from harborrag_core.topology import ChunkExtractionInput, TopologyJob
from harborrag_core.topology.derived import ContextualIndexProfile

from .vector_values import canonical_dense_vector


@dataclass(frozen=True)
class ContextualRecordBuilder:
    profile: ContextualIndexProfile

    def text(self, value: ChunkExtractionInput, description: str) -> str:
        """Prefix source text with its short semantic description for embedding only."""

        text = f"{description}\n\n{value.content}"
        if len(text.encode("utf-8")) > self.profile.max_input_bytes:
            raise ValueError("contextual embedding input exceeds explicit byte budget")
        return text

    def build(
        self,
        job: TopologyJob,
        build_id: str,
        value: ChunkExtractionInput,
        vector: tuple[float, ...],
    ) -> VectorIndexRecord:
        if len(vector) != self.profile.dimension:
            raise ValueError("contextual embedding dimension differs from pinned profile")
        identity = (
            f"contextual:{job.tenant_id}:{build_id}:{value.chunk_id}:{self.profile.fingerprint}"
        )
        point_id = str(uuid5(NAMESPACE_URL, identity))
        return VectorIndexRecord(
            id=point_id,
            tenant_id=TenantId(job.tenant_id),
            vector=list(canonical_dense_vector(vector)),
            payload={
                "record_kind": "contextual",
                "chunk_id": value.chunk_id,
                "document_id": job.document_id,
                "document_version_id": job.document_version_id,
                "build_id": build_id,
                "embedding_profile": self.profile.fingerprint,
                "config_epoch": job.config_epoch,
                "source_scope_id": job.source_scope_id,
                "projection_point_id": point_id,
            },
        )
