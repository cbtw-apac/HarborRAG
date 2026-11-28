"""Processing identity: digests of the configuration an ingestion ran with.

A run's identity must change exactly when its parsing or chunking output can change,
so a run setting enters it the same way the run's parser and chunker apply it.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from harborrag_core.ingestion import ProcessingProfile
from harborrag_core.ingestion.run_contracts import RunSettings
from harborrag_engine.ingestion.chunking import ChunkingConfig, ChunkStrategy
from harborrag_engine.ingestion.chunking.config import cap_chunking_config, pin_chunking_overlap
from harborrag_runtime.config.chunking import chunking_settings_from
from harborrag_runtime.config.errors import ParserConfigurationError
from harborrag_runtime.config.parsers import load_parser_catalog
from harborrag_runtime.config.settings import RuntimeSettings

from .chunking_profile import chunk_strategy_fingerprint
from .document.normalization import CANONICAL_NORMALIZER_VERSION

GRAPH_PROJECTION_VERSION = "graph-v5-unique-visible-edges"
"""Source assertions and metadata observations have independent document supports. Legacy
scope-owned assertions are rejected on reads until rebuilt and retired.
"""
VECTOR_PROJECTION_SCHEMA = "vector-v2"


def build_processing_profile(
    settings: RuntimeSettings,
    *,
    chunking_config: ChunkingConfig | None = None,
    chunking_strategies: tuple[ChunkStrategy, ...] = (),
    run_settings: RunSettings | None = None,
) -> ProcessingProfile:
    """Build the deterministic processing identity shared by clients and workers.

    Args:
        settings: Runtime settings naming the parser and chunking files.
        chunking_config: Profiles that replace those of ``chunking.yaml``. The
            refiner choice always comes from the file or its defaults.
        chunking_strategies: Extra chunking strategies of this deployment.
        run_settings: Per-run overrides. They change the identity only when
            they change the effective parser, its OCR or the chunking limits and
            overlap, the same way the run's parser and chunker apply them.

    Returns:
        The identity every stage of the run compares against stored work.

    Raises:
        ParserConfigurationError: The parser catalog file is missing or
            unreadable, or is invalid when a run overrides the PDF parser or OCR.
    """
    chunking = chunking_settings_from(settings)
    chunk_config = chunking_config or chunking.config
    parser_profile = _parser_catalog_digest(settings.parser_config_path)
    if run_settings is not None:
        if run_settings.chunk_max_tokens is not None:
            chunk_config = cap_chunking_config(chunk_config, run_settings.chunk_max_tokens)
        if run_settings.chunk_overlap_tokens is not None:
            chunk_config = pin_chunking_overlap(chunk_config, run_settings.chunk_overlap_tokens)
        parser_profile = _run_parser_profile(parser_profile, settings, run_settings)
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


def _run_parser_profile(base: str, settings: RuntimeSettings, run_settings: RunSettings) -> str:
    """Append the run's PDF engine and OCR choice to ``base`` when they change parsing."""
    has_ocr_override = (
        run_settings.ocr_enabled is not None or run_settings.ocr_languages is not None
    )
    if run_settings.pdf_parser is None and not has_ocr_override:
        return base
    catalog = load_parser_catalog(settings.parser_config_path)
    profile = base
    pdf_definition: str | None = None
    pdf_parser = run_settings.pdf_parser
    if pdf_parser is not None and pdf_parser != catalog.enabled_pdf_backend():
        profile = f"{profile}+pdf-{pdf_parser}"
        pdf_definition = catalog.pdf_definition_for(pdf_parser)
    if has_ocr_override and catalog.ocr_overrides_change_parsing(
        ocr_enabled=run_settings.ocr_enabled,
        ocr_languages=run_settings.ocr_languages,
        pdf_definition=pdf_definition,
    ):
        profile = f"{profile}+ocr-{_ocr_label(run_settings)}"
    return profile


def _ocr_label(run_settings: RunSettings) -> str:
    """Return ``on`` or ``off`` and the languages, such as ``off`` or ``on-en.vi``."""
    parts: list[str] = []
    if run_settings.ocr_enabled is not None:
        parts.append("on" if run_settings.ocr_enabled else "off")
    if run_settings.ocr_languages:
        parts.append(".".join(run_settings.ocr_languages))
    return "-".join(parts)


def configuration_file_digest(prefix: str, path: Path) -> str:
    """Return ``prefix`` plus a short digest of the file's bytes."""
    digest = sha256(path.read_bytes()).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _parser_catalog_digest(path: Path) -> str:
    """Digest the parser catalog, reporting a missing file as a configuration error."""
    try:
        return configuration_file_digest("parser", path)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise ParserConfigurationError(
            f"Parser configuration file cannot be read: {path} ({reason})"
        ) from error
