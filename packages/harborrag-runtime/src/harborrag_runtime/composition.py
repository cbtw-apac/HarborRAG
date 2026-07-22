"""Production composition for control-plane repositories and engine services.

Runtime is the only layer allowed to construct adapters.  The composition root
therefore owns database migration, repository wiring, health state, and resource
disposal; test doubles belong in tests or in an application development layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from harborrag_core.ports.control_plane import (
    ActivityRepositoryPort,
    JobRepositoryPort,
    MemberRepositoryPort,
    ProjectRepositoryPort,
    ProviderRepositoryPort,
    SettingsRepositoryPort,
    SourceRepositoryPort,
)
from harborrag_engine.builder import EngineBuilder

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.temporal.dependencies import RuntimeDependencies


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


@dataclass(frozen=True, slots=True)
class ControlPlaneRepositories:
    """Core-port view of the configured control-plane repositories."""

    projects: ProjectRepositoryPort
    sources: SourceRepositoryPort
    jobs: JobRepositoryPort
    activity: ActivityRepositoryPort
    settings: SettingsRepositoryPort
    providers: ProviderRepositoryPort
    members: MemberRepositoryPort


@dataclass(slots=True)
class CompositionRoot:
    """Resources assembled for one production runtime process."""

    engine_builder: EngineBuilder = field(default_factory=EngineBuilder)
    control_plane: ControlPlaneRepositories | None = None
    control_db: dict[str, Any] = field(default_factory=dict)
    mode: str = "production"
    _control_db_engine: AsyncEngine | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        """Dispose resources created by :meth:`production`."""

        if self._control_db_engine is not None:
            await self._control_db_engine.dispose()
            self._control_db_engine = None

    @classmethod
    def production(cls, settings: RuntimeSettings | None = None) -> CompositionRoot:
        """Migrate, probe, and assemble the configured control-plane database."""

        from harborrag_adapters.repositories.database.control_plane.engine import (
            create_control_plane_engine,
            create_session_factory,
        )
        from harborrag_adapters.repositories.database.control_plane.migrations import (
            run_migrations,
        )
        from harborrag_adapters.repositories.database.control_plane.repositories import (
            SqlActivityRepository,
            SqlJobRepository,
            SqlMemberRepository,
            SqlProjectRepository,
            SqlProviderRepository,
            SqlSettingsRepository,
            SqlSourceRepository,
        )
        from harborrag_core.contracts.errors import HarborConfigurationError

        from harborrag_runtime.config.settings import (
            DEFAULT_CONTROL_DB_URL,
            RuntimeSettings,
        )

        settings = settings or RuntimeSettings()
        dsn = settings.control_db_url
        if settings.env == "prod" and dsn == DEFAULT_CONTROL_DB_URL:
            raise HarborConfigurationError(
                "control_db_url is not set when HARBORRAG_ENV=prod: refusing to "
                "boot against the default local SQLite database; set "
                "HARBORRAG_CONTROL_DB_URL explicitly"
            )

        try:
            run_migrations(dsn)
        except Exception as exc:  # noqa: BLE001 - health reports boot degradation
            return cls(control_db=_failed_database_status(dsn, f"migrations failed: {exc}"))

        control_db = _probe_control_db(dsn)
        engine = create_control_plane_engine(dsn)
        sessions = create_session_factory(engine)
        repositories = ControlPlaneRepositories(
            projects=SqlProjectRepository(sessions),
            sources=SqlSourceRepository(sessions),
            jobs=SqlJobRepository(sessions),
            activity=SqlActivityRepository(sessions),
            settings=SqlSettingsRepository(sessions),
            providers=SqlProviderRepository(sessions),
            members=SqlMemberRepository(sessions),
        )
        return cls(
            control_plane=repositories,
            control_db=control_db,
            _control_db_engine=engine,
        )

    def diagnostics(self) -> dict[str, object]:
        """Return process-safe component health without exposing adapter objects."""

        return {
            "mode": self.mode,
            "runtime": {
                "provider": "production_runtime",
                "ready": self.control_db.get("ping") == "ok",
                "control_db": dict(self.control_db),
            },
            "engine": self.engine_builder.diagnostics(),
        }


def build_ingestion_dependencies(
    settings: RuntimeSettings | None = None,
) -> RuntimeDependencies:
    """Assemble the production connector-to-index worker dependency graph."""

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
    from pydantic import SecretStr

    from harborrag_runtime.config import load_connector_catalog, load_parser_catalog
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.temporal.dependencies import RuntimeDependencies
    from harborrag_runtime.temporal.ingestionstate import (
        IngestionObjectRepository,
        ObjectChunkRepository,
        ObjectManifestRepository,
        RepositoryRuntimeIngestionState,
    )

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
    dimensions = settings.embedding_dimensions or _embedding_dimensions(
        embed_config,
        embedding_model,
    )

    token_counter = Utf8TokenCounter()
    refiner = HarborChunk("recursive", token_counter)
    chunking_config = ChunkingConfig()
    chunker = build_default_chunking_service(
        config=chunking_config,
        token_counter=token_counter,
        refiner=refiner,
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
            api_key=(SecretStr(settings.qdrant_api_key) if settings.qdrant_api_key else None),
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
                SecretStr(settings.falkordb_password) if settings.falkordb_password else None
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


def _embedding_dimensions(config: Any, model_name: str) -> int:
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


def _failed_database_status(dsn: str, error: str) -> dict[str, Any]:
    return {"ping": "failed", "error": error, "scheme": dsn.split(":", 1)[0]}


def _probe_control_db(dsn: str) -> dict[str, Any]:
    """Probe connectivity and the migration stamp using a short-lived engine."""

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _probe() -> dict[str, Any]:
        engine = create_async_engine(dsn)
        try:
            async with engine.connect() as connection:
                version = (
                    await connection.execute(sa.text("SELECT version_num FROM alembic_version"))
                ).scalar()
            return {
                "ping": "ok",
                "migrations": version,
                "scheme": dsn.split(":", 1)[0],
            }
        finally:
            await engine.dispose()

    def _run() -> dict[str, Any]:
        try:
            return asyncio.run(_probe())
        except Exception as exc:  # noqa: BLE001 - diagnostics must remain available
            return _failed_database_status(dsn, str(exc))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()
