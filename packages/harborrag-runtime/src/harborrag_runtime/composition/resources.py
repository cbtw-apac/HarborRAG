from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

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
from ..errors import RuntimeConfigurationError
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
    return IngestionControlPlaneDatabase(
        client, create_schema=False, summaries_enabled=summarization_enabled(settings)
    )


def summarization_enabled(settings: RuntimeSettings) -> bool:
    """Whether summaries are produced, per config/topology/graph_build.yaml.

    Resolved here rather than by callers because the ingestion runtime builds
    its control database from raw settings, before the YAML is applied.
    """

    from harborrag_runtime.config.graph_build import GraphBuildConfig

    config = GraphBuildConfig.from_settings(settings)
    return config.effective_settings(settings).topology_parent_enabled


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


def check_embedding_dimensions(config: Any, model: str, dimensions: int) -> None:
    """Fail fast when an explicit dimension contradicts the model catalog.

    An unresolvable catalog entry is not a conflict: the explicit value is
    then the only source of truth.
    """

    try:
        expected = embedding_dimensions(config, model)
    except Exception:  # noqa: BLE001 - nothing in the catalog to contradict
        return
    if expected != dimensions:
        raise RuntimeConfigurationError(
            f"HARBORRAG_EMBEDDING_DIMENSIONS={dimensions} but model {model!r} "
            f"deployments expect {expected}"
        )


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """(scheme, host, port) of a clean http(s) URL, else ``None``.

    Never the path, query or userinfo: an unencoded ``/`` in a password moves
    part of it into the path, so anything that does not parse cleanly is
    treated as unrenderable rather than partially shown.
    """

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return None
    # An "@" beyond the netloc means a "/", "?" or "#" split the userinfo, so
    # what looks like a host or port may be a password fragment.
    if "@" in url.replace(parts.netloc, "", 1):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if not host:
        return None
    return parts.scheme, f"[{host}]" if ":" in host else host, port


def _display_url(url: str) -> str | None:
    origin = _origin(url)
    if origin is None:
        return None
    scheme, host, port = origin
    return f"{scheme}://{host}:{port}" if port is not None else f"{scheme}://{host}"


def resource_targets(settings: RuntimeSettings) -> dict[str, str]:
    """Credential-free display strings for the resources startup connects to."""

    control_url = settings.control_db_url.get_secret_value()
    if control_url.lower().startswith("sqlite"):
        control = "sqlite"
    else:
        parts = urlsplit(control_url)
        try:
            port = parts.port
        except ValueError:
            port = None
        host = parts.hostname or "unknown-host"
        control = f"postgresql {host}:{port}" if port else f"postgresql {host}"
    if settings.object_store_provider == "s3" and settings.object_store_endpoint_url:
        object_store = (
            _display_url(settings.object_store_endpoint_url) or "object store (configured URL)"
        )
    else:
        object_store = f"object store ({settings.object_store_provider})"
    origin = _origin(settings.qdrant_url)
    if origin is None:
        vector = "Qdrant (configured URL)"
    elif settings.qdrant_prefer_grpc:
        vector = f"Qdrant gRPC {origin[1]}:6334"
    else:
        vector = f"Qdrant {_display_url(settings.qdrant_url)}"
    return {
        "control": control,
        "object_store": object_store,
        "vector": vector,
        "graph": f"FalkorDB {settings.falkordb_host}:{settings.falkordb_port}",
    }


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

        return FilesystemObjectStore(
            FilesystemObjectStoreConfig(
                root=settings.object_store_root,
                namespace_prefix=settings.storage_namespace_prefix,
            )
        )
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
            namespace_prefix=settings.storage_namespace_prefix,
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
            namespace_prefix=settings.storage_namespace_prefix,
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
        namespace_prefix=settings.storage_namespace_prefix,
        max_cached_tenants=settings.falkordb_max_cached_tenants,
        read_username=settings.falkordb_read_username,
        read_password=settings.falkordb_read_password,
    )
