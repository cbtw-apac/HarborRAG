from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from harborrag_core.ingestion import ProcessingProfile
from harborrag_engine.ingestion.chunking import ChunkingConfig, ChunkStrategy
from harborrag_runtime.config.settings import RuntimeSettings

from .chunking_profile import chunk_strategy_fingerprint, default_chunking_config
from .document.normalization import CANONICAL_NORMALIZER_VERSION

# Source assertions and metadata observations have independent document supports.
# Legacy scope-owned assertions are rejected on reads until rebuilt and retired.
GRAPH_PROJECTION_VERSION = "graph-v5-unique-visible-edges"
VECTOR_PROJECTION_SCHEMA = "vector-v2"


def build_processing_profile(
    settings: RuntimeSettings,
    *,
    chunking_config: ChunkingConfig | None = None,
    chunking_strategies: tuple[ChunkStrategy, ...] = (),
) -> ProcessingProfile:
    """Build the deterministic processing identity shared by clients and workers."""

    return ProcessingProfile(
        parser_profile=configuration_file_digest(
            "parser",
            settings.parser_config_path,
        ),
        normalizer_version=CANONICAL_NORMALIZER_VERSION,
        chunk_strategy=chunk_strategy_fingerprint(
            chunking_config or default_chunking_config(), chunking_strategies
        ),
        dense_encoder_profile=settings.dense_encoder_profile,
        sparse_encoder_profile=settings.sparse_encoder_profile,
        graph_projection_version=GRAPH_PROJECTION_VERSION,
        vector_projection_schema=VECTOR_PROJECTION_SCHEMA,
    )


def configuration_file_digest(prefix: str, path: Path) -> str:
    digest = sha256(path.read_bytes()).hexdigest()[:16]
    return f"{prefix}-{digest}"
