"""Fingerprint the effective chunking inputs used by runtime and submission."""

from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from harborrag_engine.ingestion.chunking import ChunkingConfig, ChunkStrategy, ChunkStrategyRegistry
from harborrag_engine.ingestion.chunking.pipeline.composition import builtin_chunking_strategies
from harborrag_engine.ingestion.chunking.table.rendering import TableRenderer
from harborrag_runtime.config.chunking_loading import load_chunking_config
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.tokenization import ApproximateTokenCounter


def default_chunking_config() -> ChunkingConfig:
    """Return the built-in policy used when no chunking file is configured."""

    return ChunkingConfig(
        configuration_version="canonical-source-policies", create_route_chunks=True
    )


def resolve_chunking_config(settings: RuntimeSettings) -> ChunkingConfig:
    """Load the configured chunking policy, or fall back to the built-in one.

    The file is optional: deployments predating `config/chunking.yaml` keep the
    defaults, and the shipped file reproduces them exactly, so the chunk
    strategy fingerprint is the same either way. Every caller must route
    through here -- if the fingerprinted policy and the executed policy ever
    diverge, the processing identity silently misreports the corpus.
    """

    path = Path(settings.chunking_config_path).expanduser()
    if not path.is_file():
        return default_chunking_config()
    return load_chunking_config(path)


def chunk_strategy_fingerprint(
    config: ChunkingConfig,
    additional_strategies: tuple[ChunkStrategy, ...] = (),
) -> str:
    """Hash normalized policies and explicit versions, independent of mapping order."""

    counter = ApproximateTokenCounter()
    strategies = (*builtin_chunking_strategies(counter), *additional_strategies)
    registry = ChunkStrategyRegistry(strategies)
    for profile in config.profiles.values():
        registry.get(profile.strategy)
    versions = {strategy.name.strip(): strategy.version.strip() for strategy in strategies}
    if any(not version for version in versions.values()):
        raise ValueError("processing identity requires a version for every chunk strategy")
    value = {
        "configuration_version": config.configuration_version,
        "default_profile": config.default_profile,
        "create_route_chunks": config.create_route_chunks,
        "profiles": {key: asdict(profile) for key, profile in config.profiles.items()},
        "source_profiles": dict(config.source_profiles),
        "strategies": versions,
        "tokenizer": {"name": counter.name, "version": counter.version},
        "table_renderer": TableRenderer.version,
        "pipeline_version": "canonical-route-evidence-v5-bounded-route-content",
    }
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "chunking-v3-" + sha256(serialized.encode()).hexdigest()
