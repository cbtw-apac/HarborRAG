"""Strict operator policy for the deterministic chunking profiles.

The schema here is a thin transport for :class:`ChunkingConfig`. Semantic rules
-- token ordering, profile/source coherence -- stay owned by the engine
dataclasses so the file and the in-process defaults can never disagree.
"""

from __future__ import annotations

from pydantic import Field

from harborrag_core.base import StrictModel
from harborrag_engine.ingestion.chunking import (
    ChunkingConfig,
    ChunkingLimits,
    ChunkingProfile,
)

CHUNKING_CONFIG_VERSION = "canonical-source-policies"


class ChunkingLimitsConfig(StrictModel):
    """Token budget for one profile, mirroring :class:`ChunkingLimits`."""

    minimum_tokens: int = 100
    target_tokens: int = 700
    maximum_tokens: int = 1100
    overlap_tokens: int = 80
    # Omitted means "no separate packing ceiling": the engine falls back to
    # ``maximum_tokens``.
    soft_maximum_tokens: int | None = None

    def to_limits(self) -> ChunkingLimits:
        """Build the engine limits, deferring every bound check to it."""

        return ChunkingLimits(
            minimum_tokens=self.minimum_tokens,
            target_tokens=self.target_tokens,
            maximum_tokens=self.maximum_tokens,
            overlap_tokens=self.overlap_tokens,
            soft_maximum_tokens=self.soft_maximum_tokens,
        )


class ChunkingProfileConfig(StrictModel):
    """One named chunking profile. The mapping key supplies its name."""

    strategy: str
    limits: ChunkingLimitsConfig = Field(default_factory=ChunkingLimitsConfig)
    merge_small_peers: bool = True
    preserve_sections: bool = True
    repeat_table_headers: bool = True

    def to_profile(self, name: str) -> ChunkingProfile:
        """Build the engine profile bound to its configured name."""

        return ChunkingProfile(
            name=name,
            strategy=self.strategy,
            limits=self.limits.to_limits(),
            merge_small_peers=self.merge_small_peers,
            preserve_sections=self.preserve_sections,
            repeat_table_headers=self.repeat_table_headers,
        )


class ChunkingFileConfig(StrictModel):
    """Decoded ``config/chunking.yaml``.

    ``create_route_chunks`` defaults to ``True`` because that is the effective
    runtime value; it is part of the processing identity, so changing it
    re-chunks every already-ingested document.
    """

    configuration_version: str = CHUNKING_CONFIG_VERSION
    default_profile: str = "canonical"
    create_route_chunks: bool = True
    profiles: dict[str, ChunkingProfileConfig]
    source_profiles: dict[str, str] = Field(default_factory=dict)

    def to_chunking_config(self) -> ChunkingConfig:
        """Build the engine configuration, which validates coherence."""

        return ChunkingConfig(
            configuration_version=self.configuration_version,
            default_profile=self.default_profile,
            create_route_chunks=self.create_route_chunks,
            profiles={name: profile.to_profile(name) for name, profile in self.profiles.items()},
            source_profiles=dict(self.source_profiles),
        )
