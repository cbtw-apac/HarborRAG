"""Production composition for authoritative retrieval providers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
from harborrag_adapters.models.runtime import ResourceOwnership
from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    ChunkArtifactReader,
    ImmutableArtifactReader,
)
from harborrag_core.ingestion import SparseEncoderProfile
from harborrag_engine.ingestion import BM25SparseEncoder

from ..composition.resources import (
    build_ingestion_control,
    build_knowledge_graph,
    build_object_store,
    build_vector_repository,
    embedding_dimensions,
)
from ..config.graph_build import GraphBuildConfig
from ..config.settings import RuntimeSettings
from ..observability.model import IngestionTelemetry, build_model_telemetry
from ..topology.embedding_profile import build_contextual_profile
from .contextual import ContextualEvidenceSearch
from .contracts import RetrievalPolicy, RetrievalResources
from .service import RuntimeRetrievalService


async def connect_retrieval_service(
    settings: RuntimeSettings,
) -> RuntimeRetrievalService:
    """Connect Postgres, MinIO, Qdrant, and FalkorDB as one owned resource set."""

    settings = GraphBuildConfig.from_settings(settings).effective_settings(settings)
    embed_config = HarborEmbedClientConfig.from_file(settings.model_config_path)
    model = settings.embedding_model or embed_config.default_model
    dimensions = settings.embedding_dimensions or embedding_dimensions(
        embed_config,
        model,
    )
    contextual_profile = build_contextual_profile(settings, embed_config)
    telemetry = IngestionTelemetry(
        metrics_port=settings.metrics_port,
        metrics_bind_address=settings.metrics_bind_address,
    )
    embed_client = HarborEmbedClient.from_config(
        embed_config,
        telemetry=build_model_telemetry(
            embed_config,
            langfuse_enabled=settings.langfuse_enabled,
        ),
        telemetry_ownership=ResourceOwnership.OWNED,
    )
    control = build_ingestion_control(settings)
    object_store = build_object_store(settings)
    vector_repository = build_vector_repository(settings)
    graph_repository = build_knowledge_graph(settings)
    connected: list[Callable[[], Awaitable[None]]] = []
    try:
        await telemetry.start()
        connected.append(telemetry.close)
        for resource in (
            control,
            object_store,
            vector_repository,
            graph_repository,
        ):
            await resource.connect()
            connected.append(resource.close)
        await object_store.validate_buckets((ARTIFACT_BUCKET,))
    except BaseException:
        await asyncio.gather(
            *(close() for close in reversed(connected)),
            embed_client.aclose(),
            return_exceptions=True,
        )
        raise
    return RuntimeRetrievalService(
        resources=RetrievalResources(
            embed_client=embed_client,
            vector_repository=vector_repository,
            active_versions=control.document_versions,
            chunk_reader=ChunkArtifactReader(ImmutableArtifactReader(object_store)),
            sparse_encoder=BM25SparseEncoder(
                SparseEncoderProfile(
                    profile_id=settings.sparse_encoder_profile,
                    k=settings.sparse_k,
                    b=settings.sparse_b,
                    fixed_avg_len=settings.sparse_fixed_avg_len,
                )
            ),
            graph_repository=graph_repository,
            topology_repository=control.topology,
            summary_repository=control.summaries,
            contextual_search=ContextualEvidenceSearch(
                control.topology, vector_repository, contextual_profile
            ),
            document_snapshots=control.document_versions,
            source_catalog=getattr(control, "source_scans", None),
        ),
        policy=RetrievalPolicy(
            embedding_model=model,
            embedding_dimensions=dimensions,
            dense_weight=settings.retrieval_dense_weight,
            semantic_weight=settings.topology_retrieval_policy.semantic_weight,
            topology=settings.topology_retrieval_policy,
        ),
        close_resources=(
            control.close,
            object_store.close,
            vector_repository.close,
            graph_repository.close,
            embed_client.aclose,
            telemetry.close,
        ),
        telemetry=telemetry,
    )
