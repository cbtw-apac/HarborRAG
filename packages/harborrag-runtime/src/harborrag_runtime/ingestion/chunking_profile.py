"""Fingerprint the effective chunking inputs used by runtime and submission."""

from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256

from harborrag_adapters.chunking import (
    GrammarSetMismatch,
    RecursiveTextRefiner,
    build_text_refiner,
    text_refiner_fingerprint,
)
from harborrag_adapters.chunking.factory import grammar_set_problems
from harborrag_core.contracts.chunking import TextRefiner, TokenCounter
from harborrag_engine.ingestion.chunking import ChunkingConfig, ChunkStrategy, ChunkStrategyRegistry
from harborrag_engine.ingestion.chunking.pipeline.composition import builtin_chunking_strategies
from harborrag_engine.ingestion.chunking.table.rendering import TableRenderer
from harborrag_runtime.config.chunking import RefinerConfig, default_chunking_settings
from harborrag_runtime.config.errors import ChunkingConfigurationError
from harborrag_runtime.tokenization import ApproximateTokenCounter


def default_chunking_config() -> ChunkingConfig:
    return default_chunking_settings().config


def build_refiner(token_counter: TokenCounter, refiner: RefinerConfig | None = None) -> TextRefiner:
    """Build the oversized-unit refiner selected in ``chunking.yaml``."""

    refiner = refiner or RefinerConfig()
    if refiner.strategy == "recursive":
        return RecursiveTextRefiner(token_counter)
    try:
        return build_text_refiner(token_counter, require_grammars=refiner.require_code_grammars)
    except GrammarSetMismatch as error:
        raise ChunkingConfigurationError(
            "Code chunking needs the tree-sitter grammars of the declared grammar set "
            f"({'; '.join(grammar_set_problems())}). Install "
            "harborrag-adapters[chunking-code] on every process that ingests, or set "
            "refiner.require_code_grammars: false in chunking.yaml to split code at "
            "separators (the same processing identity then no longer guarantees the "
            "same code splits on every worker)."
        ) from error


def code_grammar_problems(refiner: RefinerConfig | None = None) -> list[str]:
    """Missing or mismatched tree-sitter packages the selected refiner would use.

    Empty when grammars match the declared set or the refiner does not parse code.
    """

    if (refiner or RefinerConfig()).strategy != "structure":
        return []
    return grammar_set_problems()


def refiner_fingerprint(refiner: RefinerConfig | None = None) -> str:
    """Identify the refiner's split behaviour without building it.

    Independent of installed extras (``require_code_grammars`` only decides
    whether missing grammars fail startup), so API and worker processes agree.
    """

    if (refiner or RefinerConfig()).strategy == "recursive":
        return f"{RecursiveTextRefiner.name}:{RecursiveTextRefiner.version}"
    return text_refiner_fingerprint()


def chunk_strategy_fingerprint(
    config: ChunkingConfig,
    additional_strategies: tuple[ChunkStrategy, ...] = (),
    *,
    refiner: RefinerConfig | None = None,
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
        "refiner": refiner_fingerprint(refiner),
        "table_renderer": TableRenderer.version,
        "pipeline_version": "canonical-route-evidence-v5-scoped-code-embeddings",
    }
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "chunking-v3-" + sha256(serialized.encode()).hexdigest()
