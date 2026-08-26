from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from .errors import InvalidChunkingPlanError
from .table.policy import TableChunkingPolicy


@dataclass(frozen=True, slots=True)
class ChunkingPlan:
    """Validated source-independent configuration for one chunking execution."""

    profile: str = "default"
    strategy_version: str = "1"

    create_route_chunks: bool = True
    create_evidence_chunks: bool = True

    target_tokens: int = 700
    minimum_tokens: int = 100
    soft_maximum_tokens: int = 900
    hard_maximum_tokens: int = 1100

    contextualize_embeddings: bool = True
    table_policy: TableChunkingPolicy = field(default_factory=TableChunkingPolicy)

    def __post_init__(self) -> None:
        profile = self.profile.strip()
        strategy_version = self.strategy_version.strip()
        if not profile:
            raise InvalidChunkingPlanError("profile must be non-empty")
        if not strategy_version:
            raise InvalidChunkingPlanError("strategy_version must be non-empty")
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "strategy_version", strategy_version)
        if self.minimum_tokens <= 0:
            raise InvalidChunkingPlanError("minimum_tokens must be positive")
        limits = (
            self.minimum_tokens,
            self.target_tokens,
            self.soft_maximum_tokens,
            self.hard_maximum_tokens,
        )
        if limits != tuple(sorted(limits)):
            raise InvalidChunkingPlanError(
                "token limits must satisfy minimum_tokens <= target_tokens "
                "<= soft_maximum_tokens <= hard_maximum_tokens"
            )


@dataclass(frozen=True, slots=True)
class ChunkingLimits:
    """Token limits with target, soft packing ceiling, and hard maximum."""

    minimum_tokens: int = 100
    target_tokens: int = 700
    maximum_tokens: int = 1100
    overlap_tokens: int = 80
    soft_maximum_tokens: int | None = None

    def __post_init__(self) -> None:
        soft_maximum = self.soft_maximum_tokens
        if soft_maximum is None:
            soft_maximum = self.maximum_tokens
        if self.minimum_tokens < 0:
            raise ValueError("minimum_tokens must not be negative")
        if self.target_tokens < 1:
            raise ValueError("target_tokens must be positive")
        if self.minimum_tokens > self.target_tokens:
            raise ValueError("minimum_tokens must not exceed target_tokens")
        if self.maximum_tokens < self.target_tokens:
            raise ValueError("maximum_tokens must not be below target_tokens")
        if soft_maximum < self.target_tokens:
            raise ValueError("soft_maximum_tokens must not be below target_tokens")
        if soft_maximum > self.maximum_tokens:
            raise ValueError("soft_maximum_tokens must not exceed maximum_tokens")
        if self.overlap_tokens < 0:
            raise ValueError("overlap_tokens must not be negative")
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be below target_tokens")
        object.__setattr__(self, "soft_maximum_tokens", soft_maximum)


@dataclass(frozen=True, slots=True)
class ChunkingProfile:
    """Named strategy and boundary policy for deterministic chunking."""

    name: str
    strategy: str
    limits: ChunkingLimits = field(default_factory=ChunkingLimits)
    merge_small_peers: bool = True
    preserve_sections: bool = True
    repeat_table_headers: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("profile name must be non-empty")
        if not self.strategy.strip():
            raise ValueError("profile strategy must be non-empty")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "strategy", self.strategy.strip())

    @property
    def minimum_tokens(self) -> int:
        """Return the preferred minimum chunk size."""

        return self.limits.minimum_tokens

    @property
    def target_tokens(self) -> int:
        """Return the soft target chunk size."""

        return self.limits.target_tokens

    @property
    def maximum_tokens(self) -> int:
        """Return the hard maximum chunk size."""

        return self.limits.maximum_tokens

    @property
    def soft_maximum_tokens(self) -> int:
        """Return the soft ceiling used for packing and peer merging."""

        return cast(int, self.limits.soft_maximum_tokens)

    @property
    def overlap_tokens(self) -> int:
        """Return the overlap used by text refinement."""

        return self.limits.overlap_tokens


def default_chunking_profiles() -> dict[str, ChunkingProfile]:
    """Return profiles for the canonical fallback and maintained sources."""

    return {
        "canonical": ChunkingProfile(
            name="canonical",
            strategy="canonical",
            limits=ChunkingLimits(120, 700, 1100, 0),
        ),
        "jira": ChunkingProfile(
            name="jira",
            strategy="jira",
            limits=ChunkingLimits(80, 600, 1000, 40),
        ),
        "confluence": ChunkingProfile(
            name="confluence",
            strategy="confluence",
            limits=ChunkingLimits(120, 750, 1200, 0),
        ),
    }


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """Immutable source-to-profile configuration.

    Chunking receives a canonical document, so media-type routing belongs to
    parsing and normalization. The only maintained source policies here are
    Confluence and Jira; every other source uses the canonical fallback.
    """

    configuration_version: str = "canonical-source-policies"
    default_profile: str = "canonical"
    create_route_chunks: bool = False
    profiles: Mapping[str, ChunkingProfile] = field(default_factory=default_chunking_profiles)
    source_profiles: Mapping[str, str] = field(
        default_factory=lambda: {
            "confluence": "confluence",
            "jira": "jira",
        }
    )

    def __post_init__(self) -> None:
        profiles = dict(self.profiles)
        source_profiles = {
            source.strip().lower(): profile.strip()
            for source, profile in self.source_profiles.items()
        }
        if not self.configuration_version.strip():
            raise ValueError("configuration_version must be non-empty")
        if self.default_profile not in profiles:
            raise ValueError("default_profile must name a configured profile")
        for key, profile in profiles.items():
            if key != profile.name:
                raise ValueError(f"profile key/name mismatch: {key!r} != {profile.name!r}")
        if any(not source or not profile for source, profile in source_profiles.items()):
            raise ValueError("source profile names must be non-empty")
        unknown_profiles = set(source_profiles.values()).difference(profiles)
        if unknown_profiles:
            names = ", ".join(sorted(unknown_profiles))
            raise ValueError(f"source mapping references unknown profiles: {names}")
        object.__setattr__(self, "profiles", MappingProxyType(profiles))
        object.__setattr__(self, "source_profiles", MappingProxyType(source_profiles))

    def profile_for(self, source_kind: str, override: str | None = None) -> ChunkingProfile:
        """Resolve a named profile without inspecting raw media types."""

        profile_name = override or self.source_profiles.get(
            source_kind.strip().lower(),
            self.default_profile,
        )
        try:
            return self.profiles[profile_name]
        except KeyError as exc:
            raise ValueError(f"unknown chunking profile: {profile_name}") from exc
