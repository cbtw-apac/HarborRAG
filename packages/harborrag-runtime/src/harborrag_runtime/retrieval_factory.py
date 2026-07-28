"""Lazy production composition for runtime retrieval providers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic import SecretStr

from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
from harborrag_adapters.repositories.graph import HarborGraphDBClient
from harborrag_adapters.repositories.graph.falkordb import FalkorDBGraphConfig
from harborrag_adapters.repositories.object_store.filesystem import (
    FilesystemObjectStore,
    FilesystemObjectStoreConfig,
)
from harborrag_adapters.repositories.vector import HarborVectorDBClient
from harborrag_adapters.repositories.vector.qdrant import QdrantVectorConfig
from harborrag_engine.ingestion.indexing.config import IndexingConfig

from .config.settings import RuntimeSettings
from .ingestion_dependencies import embedding_dimensions
from .retrieval import RetrievalResources, RuntimeRetrievalService
from .temporal.artifact_objects import IngestionObjectRepository, ObjectChunkRepository


async def connect_retrieval_service(
    settings: RuntimeSettings,
) -> RuntimeRetrievalService:
    """Construct providers and close every connected resource on partial failure."""

    embed_config = HarborEmbedClientConfig.from_file(settings.model_config_path)
    model = settings.embedding_model or embed_config.default_model
    dimensions = settings.embedding_dimensions or embedding_dimensions(
        embed_config,
        model,
    )
    embed_client = HarborEmbedClient.from_config(embed_config)
    vector_repository = HarborVectorDBClient.default().create_from_config(
        QdrantVectorConfig(
            url=settings.qdrant_url,
            api_key=SecretStr(settings.qdrant_api_key) if settings.qdrant_api_key else None,
            prefer_grpc=settings.qdrant_prefer_grpc,
            collection_prefix=settings.qdrant_collection_prefix,
            allow_insecure_remote=settings.qdrant_allow_insecure_remote,
        )
    )
    graph_repository = HarborGraphDBClient.default().create_from_config(
        FalkorDBGraphConfig(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            username=settings.falkordb_username,
            password=(
                SecretStr(settings.falkordb_password) if settings.falkordb_password else None
            ),
            graph_name=settings.falkordb_graph,
            ssl=settings.falkordb_ssl,
            allow_insecure_remote=settings.falkordb_allow_insecure_remote,
        )
    )
    object_store = FilesystemObjectStore(
        FilesystemObjectStoreConfig(root=settings.ingestion_object_root)
    )
    connected: list[Callable[[], Awaitable[None]]] = []
    try:
        for resource in (object_store, vector_repository, graph_repository):
            await resource.connect()
            connected.append(resource.close)
    except BaseException:
        await asyncio.gather(
            *(close() for close in reversed(connected)),
            embed_client.aclose(),
            return_exceptions=True,
        )
        raise
    objects = IngestionObjectRepository(object_store)
    return RuntimeRetrievalService(
        resources=RetrievalResources(
            embed_client=embed_client,
            vector_repository=vector_repository,
            graph_repository=graph_repository,
            chunk_repository=ObjectChunkRepository(objects),
        ),
        indexing_config=IndexingConfig(
            embedding_model=model,
            embedding_dimensions=dimensions,
            vector_collection=settings.vector_collection,
            graph_namespace=settings.graph_namespace,
        ),
        close_resources=(
            object_store.close,
            vector_repository.close,
            graph_repository.close,
            embed_client.aclose,
        ),
    )
