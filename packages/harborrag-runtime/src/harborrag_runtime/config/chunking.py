"""Versioned chunking policy: profiles, token limits, source mapping and refiner.

``config/ingestion/chunking.yaml`` is optional. Without it (and without an explicit
``chunking_config_path``) the built-in defaults below apply, which are the same
values the shipped file declares. API and worker processes both resolve the
policy through :func:`chunking_settings_from`, so they compute the same
processing identity from the same file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, ValidationError

from harborrag_core.base import StrictModel
from harborrag_engine.ingestion.chunking import ChunkingConfig
from harborrag_engine.ingestion.chunking.config import ChunkingLimits, ChunkingProfile
from harborrag_runtime.config.errors import ChunkingConfigurationError
from harborrag_runtime.config.loading import read_yaml_file, require_string_mapping

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings

CHUNKING_CONFIG_VERSION = 1
DEFAULT_CONFIGURATION_VERSION = "canonical-source-policies"


class ChunkingLimitsConfig(StrictModel):
    """Token limits of one profile; see ``ChunkingLimits`` for the ordering rules."""

    minimum_tokens: int = Field(ge=0)
    target_tokens: int = Field(ge=1)
    maximum_tokens: int = Field(ge=1)
    overlap_tokens: int = Field(default=0, ge=0)
    soft_maximum_tokens: int | None = Field(default=None, ge=1)


class ChunkingProfileConfig(StrictModel):
    """One named chunking profile."""

    strategy: str = Field(min_length=1)
    limits: ChunkingLimitsConfig
    merge_small_peers: bool = True
    preserve_sections: bool = True
    repeat_table_headers: bool = True


class RefinerConfig(StrictModel):
    """How a unit still above its profile's ``maximum_tokens`` is split.

    ``structure`` routes code, Markdown, prose and line-oriented units to the
    matching structure-aware refiner. ``recursive`` is the LangChain recursive
    character splitter (needs ``harborrag-adapters[chunking]``).
    ``require_code_grammars`` (default) fails startup when the tree-sitter
    grammars of ``harborrag-adapters[chunking-code]`` are missing or at another
    version. The processing identity declares the grammar set without looking
    at what is installed, so only then does the same identity mean the same
    code splits on every worker. ``false`` splits code at separators where
    grammars are missing and gives that guarantee up.
    """

    strategy: Literal["structure", "recursive"] = "structure"
    require_code_grammars: bool = True


class ChunkingFileConfig(StrictModel):
    """Root of ``chunking.yaml``. Omitted sections keep the built-in defaults."""

    version: Literal[1]
    configuration_version: str = Field(default=DEFAULT_CONFIGURATION_VERSION, min_length=1)
    default_profile: str | None = Field(default=None, min_length=1)
    create_route_chunks: bool = True
    refiner: RefinerConfig = Field(default_factory=RefinerConfig)
    profiles: dict[str, ChunkingProfileConfig] | None = None
    source_profiles: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ChunkingSettings:
    """Resolved chunking policy plus the refiner choice."""

    config: ChunkingConfig
    refiner: RefinerConfig = field(default_factory=RefinerConfig)
    source_path: Path | None = None


def default_chunking_settings() -> ChunkingSettings:
    """Return the built-in policy used when no chunking file is configured."""

    return ChunkingSettings(
        config=ChunkingConfig(
            configuration_version=DEFAULT_CONFIGURATION_VERSION,
            create_route_chunks=True,
        )
    )


def load_chunking_settings(path: str | Path) -> ChunkingSettings:
    """Load ``chunking.yaml``, rejecting duplicate/unknown keys and invalid limits."""

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
        parsed = ChunkingFileConfig.model_validate(root, strict=True)
        config = _engine_config(parsed)
    except (ValidationError, ValueError) as error:
        raise ChunkingConfigurationError(
            f"Invalid chunking configuration {source}: {error}"
        ) from error
    return ChunkingSettings(config=config, refiner=parsed.refiner, source_path=source)


def chunking_settings_from(settings: RuntimeSettings) -> ChunkingSettings:
    """Load the configured file, or the defaults when the default path is absent."""

    configured = Path(settings.chunking_config_path).expanduser()
    if configured.is_file() or "chunking_config_path" in settings.model_fields_set:
        return load_chunking_settings(configured)
    return default_chunking_settings()


def _engine_config(parsed: ChunkingFileConfig) -> ChunkingConfig:
    defaults = ChunkingConfig()
    profiles = (
        dict(defaults.profiles)
        if parsed.profiles is None
        else {name: _profile(name, profile) for name, profile in parsed.profiles.items()}
    )
    return ChunkingConfig(
        configuration_version=parsed.configuration_version,
        default_profile=parsed.default_profile or defaults.default_profile,
        create_route_chunks=parsed.create_route_chunks,
        profiles=profiles,
        source_profiles=(
            dict(defaults.source_profiles)
            if parsed.source_profiles is None
            else dict(parsed.source_profiles)
        ),
    )


def _profile(name: str, profile: ChunkingProfileConfig) -> ChunkingProfile:
    limits = profile.limits
    return ChunkingProfile(
        name=name,
        strategy=profile.strategy,
        limits=ChunkingLimits(
            minimum_tokens=limits.minimum_tokens,
            target_tokens=limits.target_tokens,
            maximum_tokens=limits.maximum_tokens,
            overlap_tokens=limits.overlap_tokens,
            soft_maximum_tokens=limits.soft_maximum_tokens,
        ),
        merge_small_peers=profile.merge_small_peers,
        preserve_sections=profile.preserve_sections,
        repeat_table_headers=profile.repeat_table_headers,
    )
