"""Strict loader for the versioned graph-build YAML policy."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from harborrag_runtime.config.errors import GraphBuildConfigurationError
from harborrag_runtime.config.graph_build import GraphBuildConfig
from harborrag_runtime.config.loading import read_yaml_file, require_string_mapping


def load_graph_build_config(path: str | Path) -> GraphBuildConfig:
    """Decode YAML once, reject duplicate/unknown keys, and validate every bound."""

    source, raw = read_yaml_file(
        path,
        label="Graph-build configuration",
        error_type=GraphBuildConfigurationError,
    )
    root = require_string_mapping(
        raw,
        label="graph-build configuration root",
        error_type=GraphBuildConfigurationError,
    )
    try:
        return GraphBuildConfig.model_validate(root, strict=True)
    except ValidationError as error:
        raise GraphBuildConfigurationError(
            f"Invalid graph-build configuration {source}: {error}"
        ) from error
