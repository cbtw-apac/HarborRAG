"""Strict loader for the chunking policy YAML."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from harborrag_engine.ingestion.chunking import ChunkingConfig
from harborrag_runtime.config.chunking import ChunkingFileConfig
from harborrag_runtime.config.errors import ChunkingConfigurationError
from harborrag_runtime.config.loading import read_yaml_file, require_string_mapping


def load_chunking_config(path: str | Path) -> ChunkingConfig:
    """Decode YAML once, reject unknown keys, and validate every bound."""

    source, raw = read_yaml_file(
        path,
        label="Chunking configuration",
        error_type=ChunkingConfigurationError,
    )
    root = require_string_mapping(
        raw,
        label="chunking configuration root",
        error_type=ChunkingConfigurationError,
    )
    try:
        config = ChunkingFileConfig.model_validate(root, strict=True)
    except ValidationError as error:
        raise ChunkingConfigurationError(
            f"Invalid chunking configuration {source}: {error}"
        ) from error
    try:
        # Token ordering and profile/source coherence belong to the engine
        # dataclasses; surface their complaints against the file that caused them.
        return config.to_chunking_config()
    except ValueError as error:
        raise ChunkingConfigurationError(
            f"Invalid chunking configuration {source}: {error}"
        ) from error
