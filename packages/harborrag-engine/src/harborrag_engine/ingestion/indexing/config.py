from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from harborrag_core.schemas.vector import VectorDistance


class RemovedVectorPolicy(StrEnum):
    """Describe deferred handling for vectors absent from a new revision."""

    RETIRE = "retire"
    DELETE = "delete"
    TOMBSTONE = "tombstone"


DEFAULT_VECTOR_METADATA_INDEXES = (
    "generation_id",
    "logical_chunk_id",
    "artifact_id",
    "artifact_revision_id",
    "source_kind",
    "chunk_role",
    "content_hash",
    "embedding_configuration_fingerprint",
    "index_state",
    "is_active",
)


@dataclass(frozen=True, slots=True)
class IndexingConfig:
    """One cohesive policy for embedding, vector, and graph indexing."""

    embedding_model: str
    embedding_dimensions: int
    vector_collection: str
    graph_namespace: str
    embedding_batch_size: int = 64
    maximum_embedding_batch_tokens: int = 8192
    embedding_concurrency: int = 4
    embedding_context_maximum_characters: int = 512
    embedding_text_rendering_version: str = "1"
    normalize_embeddings: bool = True
    capsule_maximum_characters: int = 512
    include_chunk_content_in_vector_payload: bool = False
    removed_vector_policy: RemovedVectorPolicy = RemovedVectorPolicy.RETIRE
    vector_distance: VectorDistance = VectorDistance.COSINE
    vector_metadata_indexes: tuple[str, ...] = DEFAULT_VECTOR_METADATA_INDEXES

    def __post_init__(self) -> None:
        """Validate indexing identities, limits, and vector metadata fields."""

        identity_values = (
            self.embedding_model,
            self.vector_collection,
            self.graph_namespace,
            self.embedding_text_rendering_version,
        )
        if not all(value.strip() for value in identity_values):
            raise ValueError("indexing configuration identity values must be non-empty")
        positive_values = (
            self.embedding_dimensions,
            self.embedding_batch_size,
            self.maximum_embedding_batch_tokens,
            self.embedding_concurrency,
            self.embedding_context_maximum_characters,
            self.capsule_maximum_characters,
        )
        if any(value < 1 for value in positive_values):
            raise ValueError("indexing configuration limits must be positive")
        if any(not field.strip() for field in self.vector_metadata_indexes):
            raise ValueError("vector metadata index names must be non-empty")
        if len(set(self.vector_metadata_indexes)) != len(self.vector_metadata_indexes):
            raise ValueError("vector metadata index names must be unique")

    @property
    def embedding_configuration_fingerprint(self) -> str:
        """Fingerprint every provider-independent choice affecting vector identity."""

        payload = json.dumps(
            {
                "dimensions": self.embedding_dimensions,
                "context_maximum_characters": (self.embedding_context_maximum_characters),
                "model": self.embedding_model,
                "normalize": self.normalize_embeddings,
                "rendering_version": self.embedding_text_rendering_version,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    @property
    def configuration_fingerprint(self) -> str:
        """Fingerprint all policy that can change persisted index behavior."""

        payload = json.dumps(
            {
                "embedding_batch_size": self.embedding_batch_size,
                "embedding_concurrency": self.embedding_concurrency,
                "embedding_configuration_fingerprint": (self.embedding_configuration_fingerprint),
                "include_chunk_content_in_vector_payload": (
                    self.include_chunk_content_in_vector_payload
                ),
                "maximum_embedding_batch_tokens": self.maximum_embedding_batch_tokens,
                "capsule_maximum_characters": self.capsule_maximum_characters,
                "graph_namespace": self.graph_namespace,
                "removed_vector_policy": self.removed_vector_policy.value,
                "vector_collection": self.vector_collection,
                "vector_distance": self.vector_distance.value,
                "vector_metadata_indexes": self.vector_metadata_indexes,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(payload.encode("utf-8")).hexdigest()
