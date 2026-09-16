from __future__ import annotations

from typing import Any

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.graph.falkordb import (
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_adapters.repositories.graph.falkordb.topology import FalkorTopologyRepository
from harborrag_adapters.repositories.object_store.s3 import (
    S3ObjectStore,
    S3ObjectStoreConfig,
)
from harborrag_adapters.repositories.vector import (
    HarborVectorDBClient,
)
from harborrag_adapters.repositories.vector.qdrant import QdrantVectorConfig
from harborrag_core.ports.storage import ObjectStorePort, VectorRepositoryPort

from ..config.settings import RuntimeSettings
from .storage_providers import (
    RuntimeKnowledgeGraphPort,
    RuntimeTopologyPort,
    storage_providers,
)


def build_ingestion_control(
    settings: RuntimeSettings,
) -> IngestionControlPlaneDatabase:
    """Create the Postgres authority without importing projection providers."""

    url = settings.control_db_url.get_secret_value()
    backend = "postgresql" if url.startswith("postgresql+asyncpg://") else "sqlite"
    client = SQLAlchemyDBClient(
        backend=backend,
        url=url,
        pool_size=(settings.control_db_pool_size if backend == "postgresql" else None),
        max_overflow=(settings.control_db_max_overflow if backend == "postgresql" else None),
        pool_recycle_seconds=1800,
        echo=False,
    )
    return IngestionControlPlaneDatabase(client, create_schema=False)


def embedding_dimensions(config: Any, model_name: str) -> int:
    """Resolve one unambiguous embedding dimension from the model catalog."""

    _, model = config.model_for(model_name)
    expected = {
        deployment.expected_dimensions
        for deployment in model.deployments
        if deployment.expected_dimensions is not None
    }
    if len(expected) == 1:
        return int(expected.pop())
    if model.default_params.dimensions is not None:
        return int(model.default_params.dimensions)
    raise ValueError(
        f"embedding model {model_name!r} has no unambiguous expected_dimensions; "
        "set HARBORRAG_EMBEDDING_DIMENSIONS"
    )


def build_object_store(settings: RuntimeSettings) -> ObjectStorePort:
    """Resolve the configured immutable artifact provider."""

    if settings.object_store_provider == "memory":
        from harborrag_adapters.repositories.object_store.memory import (
            MemoryObjectStore,
        )

        return MemoryObjectStore()
    if settings.object_store_provider == "filesystem":
        from harborrag_adapters.repositories.object_store.filesystem import (
            FilesystemObjectStore,
            FilesystemObjectStoreConfig,
        )

        return FilesystemObjectStore(FilesystemObjectStoreConfig(root=settings.object_store_root))
    if settings.object_store_provider != "s3":
        return storage_providers.object_store(settings)

    return S3ObjectStore(
        S3ObjectStoreConfig(
            endpoint_url=settings.object_store_endpoint_url,
            region=settings.object_store_region,
            access_key_id=settings.object_store_access_key_id,
            secret_access_key=settings.object_store_secret_access_key,
            session_token=settings.object_store_session_token,
            allow_insecure_remote=settings.object_store_allow_insecure_remote,
        )
    )


def build_vector_repository(
    settings: RuntimeSettings,
) -> VectorRepositoryPort:
    """Resolve the configured vector projection provider."""

    if settings.vector_provider != "qdrant":
        return storage_providers.vector_repository(settings)

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
    """Resolve the configured document-versioned graph provider."""

    if settings.graph_provider != "falkordb":
        return storage_providers.knowledge_graph(settings)

    return FalkorKnowledgeGraphRepository(build_graph_config(settings))


def build_topology_repository(settings: RuntimeSettings) -> RuntimeTopologyPort:
    """Resolve topology through the same provider choice as the knowledge graph."""

    if settings.graph_provider != "falkordb":
        return storage_providers.topology(settings)
    return FalkorTopologyRepository(build_graph_config(settings))


def build_graph_config(settings: RuntimeSettings) -> FalkorDBGraphConfig:
    """Trusted tenant registry routing is mandatory in production composition."""
    return FalkorDBGraphConfig(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        username=settings.falkordb_username,
        password=settings.falkordb_password,
        graph_name=settings.falkordb_graph,
        ssl=settings.falkordb_ssl,
        max_connections=settings.falkordb_max_connections,
        allow_insecure_remote=settings.falkordb_allow_insecure_remote,
        tenant_isolation=True,
        tenant_graph_prefix=settings.falkordb_tenant_graph_prefix,
        max_cached_tenants=settings.falkordb_max_cached_tenants,
        read_username=settings.falkordb_read_username,
        read_password=settings.falkordb_read_password,
    )
