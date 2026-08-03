from __future__ import annotations

from typing import Protocol

from harborrag_adapters.repositories.graph.falkordb import (
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_adapters.repositories.object_store.s3 import (
    S3ObjectStore,
    S3ObjectStoreConfig,
)
from harborrag_adapters.repositories.vector import (
    HarborVectorDBClient,
    HarborVectorRepository,
)
from harborrag_adapters.repositories.vector.qdrant import QdrantVectorConfig
from harborrag_core.ports import (
    GraphRetrievalRepositoryPort,
    KnowledgeGraphRepositoryPort,
)

from .config.settings import RuntimeSettings


class RuntimeKnowledgeGraphPort(
    KnowledgeGraphRepositoryPort,
    GraphRetrievalRepositoryPort,
    Protocol,
):
    """Combined write and retrieval capabilities required by runtime composition."""


def build_object_store(settings: RuntimeSettings) -> S3ObjectStore:
    """Create the MinIO/S3 immutable artifact repository."""

    return S3ObjectStore(
        S3ObjectStoreConfig(
            endpoint_url=settings.object_store_endpoint_url,
            region=settings.object_store_region,
            access_key_id=settings.object_store_access_key_id,
            secret_access_key=settings.object_store_secret_access_key,
            session_token=settings.object_store_session_token,
        )
    )


def build_vector_repository(
    settings: RuntimeSettings,
) -> HarborVectorRepository:
    """Create the Qdrant dense/sparse projection repository."""

    return HarborVectorDBClient.default().create_from_config(
        QdrantVectorConfig(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            prefer_grpc=settings.qdrant_prefer_grpc,
            collection_prefix=settings.qdrant_collection_prefix,
            allow_insecure_remote=settings.qdrant_allow_insecure_remote,
        )
    )


def build_knowledge_graph(
    settings: RuntimeSettings,
) -> RuntimeKnowledgeGraphPort:
    """Create the document-versioned, non-LLM FalkorDB projection repository."""

    return FalkorKnowledgeGraphRepository(
        FalkorDBGraphConfig(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            username=settings.falkordb_username,
            password=settings.falkordb_password,
            graph_name=settings.falkordb_graph,
            ssl=settings.falkordb_ssl,
            max_connections=settings.falkordb_max_connections,
            allow_insecure_remote=settings.falkordb_allow_insecure_remote,
        )
    )
