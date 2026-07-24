"""Production adapter composition for Temporal ingestion workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .config.settings import RuntimeSettings
    from .temporal.dependencies import RuntimeDependencies


@dataclass(slots=True)
class RepositoryResource:
    """Adapt repository connect/close lifecycles to the runtime lifecycle port."""

    connect: Callable[[], Awaitable[None]]
    dispose: Callable[[], Awaitable[None]]

    async def start(self) -> None:
        task: asyncio.Future[None] = asyncio.ensure_future(self.connect())
        while not task.done():
            await asyncio.sleep(0.01)
        await task

    async def close(self) -> None:
        await self.dispose()


@dataclass(slots=True)
class AsyncCloseResource:
    """Own an async-close-only resource in a runtime dependency graph."""

    dispose: Callable[[], Awaitable[None]]

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        await self.dispose()


class Utf8TokenCounter:
    """Use UTF-8 bytes as a conservative, provider-neutral token upper bound."""

    def count(self, text: str) -> int:
        return len(text.encode("utf-8"))


def build_ingestion_dependencies(
    settings: RuntimeSettings | None = None,
) -> RuntimeDependencies:
    """Assemble the production connector-to-index worker dependency graph."""

    from pydantic import SecretStr

    from harborrag_adapters.chunking import HarborChunk
    from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
    from harborrag_adapters.repositories.graph import HarborGraphDBClient
    from harborrag_adapters.repositories.graph.falkordb import FalkorDBGraphConfig
    from harborrag_adapters.repositories.object_store.filesystem import (
        FilesystemObjectStore,
        FilesystemObjectStoreConfig,
    )
    from harborrag_adapters.repositories.state.sqlite import (
        SQLiteStateBackend,
        SQLiteStateConfig,
    )
    from harborrag_adapters.repositories.vector import HarborVectorDBClient
    from harborrag_adapters.repositories.vector.qdrant import QdrantVectorConfig
    from harborrag_core.ports.indexing import (
        GraphGenerationRepositoryPort,
        VectorGenerationRepositoryPort,
    )
    from harborrag_engine.ingestion import (
        ChunkingConfig,
        ChunkPersistenceService,
        DocumentNormalizer,
        build_default_chunking_service,
    )
    from harborrag_engine.ingestion.indexing import (
        GraphIndexService,
        IndexGenerationActivationService,
        IndexingConfig,
        IndexingService,
        VectorIndexService,
    )

    from .config import load_connector_catalog, load_parser_catalog
    from .config.settings import RuntimeSettings
    from .temporal.artifact_objects import (
        IngestionObjectRepository,
        ObjectChunkRepository,
        ObjectManifestRepository,
    )
    from .temporal.dependencies import RuntimeDependencies
    from .temporal.ingestionstate import RepositoryRuntimeIngestionState

    settings = settings or RuntimeSettings()
    parser = load_parser_catalog(settings.parser_config_path).build_harbor_parser()
    connector_catalog = load_connector_catalog(settings.connector_config_path)
    connectors = {
        name: connector_catalog.build(
            name,
            connector_kwargs=(
                {"parser": parser}
                if connector_catalog.get(name).provider in {"confluence", "jira"}
                else None
            ),
        )
        for name in connector_catalog.names(enabled_only=True)
    }
    embed_config = HarborEmbedClientConfig.from_file(settings.model_config_path)
    embedding_model = settings.embedding_model or embed_config.default_model
    dimensions = settings.embedding_dimensions or embedding_dimensions(
        embed_config,
        embedding_model,
    )

    token_counter = Utf8TokenCounter()
    refiner = HarborChunk("recursive", token_counter)
    markdown_splitter = (
        HarborChunk("markdown", token_counter)
        if HarborChunk.available("markdown")
        else None
    )
    html_splitter = (
        HarborChunk("html", token_counter) if HarborChunk.available("html") else None
    )
    json_splitter = (
        HarborChunk("json", token_counter) if HarborChunk.available("json") else None
    )
    chunking_config = ChunkingConfig()
    chunker = build_default_chunking_service(
        config=chunking_config,
        token_counter=token_counter,
        refiner=refiner,
        markdown_splitter=markdown_splitter,
        html_splitter=html_splitter,
        json_splitter=json_splitter,
    )

    state_backend = SQLiteStateBackend(
        SQLiteStateConfig(
            database=str(settings.ingestion_state_database),
            create_schema=True,
        )
    )
    object_store = FilesystemObjectStore(
        FilesystemObjectStoreConfig(root=settings.ingestion_object_root)
    )
    objects = IngestionObjectRepository(object_store)
    chunks = ObjectChunkRepository(objects)
    manifests = ObjectManifestRepository(objects)

    embed_client = HarborEmbedClient.from_config(embed_config)
    vector_repository = HarborVectorDBClient.default().create_from_config(
        QdrantVectorConfig(
            url=settings.qdrant_url,
            api_key=(
                SecretStr(settings.qdrant_api_key)
                if settings.qdrant_api_key
                else None
            ),
            prefer_grpc=settings.qdrant_prefer_grpc,
            collection_prefix=settings.qdrant_collection_prefix,
        )
    )
    graph_repository = HarborGraphDBClient.default().create_from_config(
        FalkorDBGraphConfig(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            username=settings.falkordb_username,
            password=(
                SecretStr(settings.falkordb_password)
                if settings.falkordb_password
                else None
            ),
            graph_name=settings.falkordb_graph,
            ssl=settings.falkordb_ssl,
        )
    )
    indexing_config = IndexingConfig(
        embedding_model=embedding_model,
        embedding_dimensions=dimensions,
        vector_collection=settings.vector_collection,
        graph_namespace=settings.graph_namespace,
    )
    activator = IndexGenerationActivationService(
        cast(VectorGenerationRepositoryPort, vector_repository),
        cast(GraphGenerationRepositoryPort, graph_repository),
    )
    state = RepositoryRuntimeIngestionState(
        state_backend,
        objects,
        indexing_config,
        activator,
        chunking_configuration_version=chunking_config.configuration_version,
    )
    indexer = IndexingService(
        vector_service=VectorIndexService(
            embed_client=embed_client,
            vector_repository=vector_repository,
            token_counter=token_counter,
        ),
        graph_service=GraphIndexService(graph_repository=graph_repository),
    )
    resources = (
        RepositoryResource(state_backend.connect, state_backend.close),
        RepositoryResource(object_store.connect, object_store.close),
        RepositoryResource(vector_repository.connect, vector_repository.close),
        RepositoryResource(graph_repository.connect, graph_repository.close),
        AsyncCloseResource(embed_client.aclose),
    )
    return RuntimeDependencies(
        connectors=connectors,
        parser=parser,
        normalizer=DocumentNormalizer(),
        chunker=chunker,
        chunk_persistence=ChunkPersistenceService(chunks, manifests),
        indexer=indexer,
        state=state,
        resources=resources,
    )


def embedding_dimensions(config: Any, model_name: str) -> int:
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
