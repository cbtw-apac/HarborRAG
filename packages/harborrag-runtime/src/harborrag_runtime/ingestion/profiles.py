from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from harborrag_core.ingestion import ProcessingProfile
from harborrag_engine.ingestion.chunking import ChunkingConfig, ChunkStrategy
from harborrag_engine.ingestion.chunking.config import cap_chunking_config
from harborrag_runtime.config.chunking import chunking_settings_from
from harborrag_runtime.config.parsers import load_parser_catalog
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion_contracts import RunSettings

from .chunking_profile import chunk_strategy_fingerprint
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
    run_settings: RunSettings | None = None,
) -> ProcessingProfile:
    """Build the deterministic processing identity shared by clients and workers.

    ``chunking_config`` overrides the profiles of ``chunking.yaml``; the refiner
    choice always comes from the file (or its defaults). ``run_settings`` overrides
    change the identity only when they change the effective parser or limits.
    """

    chunking = chunking_settings_from(settings)
    chunk_config = chunking_config or chunking.config
    parser_profile = configuration_file_digest("parser", settings.parser_config_path)
    if run_settings is not None:
        if run_settings.chunk_max_tokens is not None:
            chunk_config = cap_chunking_config(chunk_config, run_settings.chunk_max_tokens)
        pdf_parser = run_settings.pdf_parser
        if (
            pdf_parser is not None
            and pdf_parser != load_parser_catalog(settings.parser_config_path).enabled_pdf_backend()
        ):
            parser_profile = f"{parser_profile}+pdf-{pdf_parser}"
    return ProcessingProfile(
        parser_profile=parser_profile,
        normalizer_version=CANONICAL_NORMALIZER_VERSION,
        chunk_strategy=chunk_strategy_fingerprint(
            chunk_config,
            chunking_strategies,
            refiner=chunking.refiner,
        ),
        dense_encoder_profile=settings.dense_encoder_profile,
        sparse_encoder_profile=settings.sparse_encoder_profile,
        graph_projection_version=GRAPH_PROJECTION_VERSION,
        vector_projection_schema=VECTOR_PROJECTION_SCHEMA,
    )


def configuration_file_digest(prefix: str, path: Path) -> str:
    digest = sha256(path.read_bytes()).hexdigest()[:16]
    return f"{prefix}-{digest}"
