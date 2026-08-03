from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from .errors import InvalidChunkingPlanError
from .table_policy import TableChunkingPolicy


@dataclass(frozen=True, slots=True)
class ChunkingPlan:
    """Validated source-independent configuration for one chunking execution."""

    profile: str = "default"
    strategy_version: str = "1"

    create_route_chunks: bool = True
    create_context_parents: bool = True
    create_evidence_chunks: bool = True

    target_tokens: int = 700
    minimum_tokens: int = 100
    soft_maximum_tokens: int = 900
    hard_maximum_tokens: int = 1100
    boundary_overlap_sentences: int = 0

    index_comments: bool = True
    index_events: bool = True
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
        if self.boundary_overlap_sentences < 0:
            raise InvalidChunkingPlanError("boundary_overlap_sentences must not be negative")


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
    preserve_tables: bool = True
    preserve_code_blocks: bool = True
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
    """Return independent default profiles for supported source families."""

    return {
        "generic": ChunkingProfile(
            name="generic",
            strategy="generic",
            limits=ChunkingLimits(100, 700, 1100, 80),
        ),
        "document": ChunkingProfile(
            name="document",
            strategy="document",
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
        # Code currently uses the document strategy's block/line preservation.
        # A dedicated strategy is registered only when Tree-sitter is available.
        "code": ChunkingProfile(
            name="code",
            strategy="document",
            limits=ChunkingLimits(60, 500, 900, 0),
        ),
        "json": ChunkingProfile(
            name="json",
            strategy="json",
            limits=ChunkingLimits(50, 600, 1000, 0),
        ),
    }


@dataclass(frozen=True, slots=True)
class ChunkRoute:
    """Select a profile using normalized request attributes."""

    profile: str
    source_kind: str | None = None
    content_type: str | None = None
    content_category: str | None = None

    def __post_init__(self) -> None:
        if not self.profile.strip():
            raise ValueError("route profile must be non-empty")
        object.__setattr__(self, "profile", self.profile.strip())
        if self.source_kind is not None:
            object.__setattr__(self, "source_kind", self.source_kind.strip().lower())
        if self.content_type is not None:
            content_type = self.content_type.split(";", 1)[0].strip().lower()
            object.__setattr__(self, "content_type", content_type)
        if self.content_category is not None:
            object.__setattr__(
                self,
                "content_category",
                self.content_category.strip().lower(),
            )
        if not any((self.source_kind, self.content_type, self.content_category)):
            raise ValueError("a route must define at least one match condition")

    def matches(
        self,
        *,
        source_kind: str,
        content_type: str,
        content_category: str,
    ) -> bool:
        """Return whether all configured route conditions match the request."""

        return (
            (self.source_kind is None or self.source_kind == source_kind)
            and (self.content_type is None or self.content_type == content_type)
            and (self.content_category is None or self.content_category == content_category)
        )


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """Immutable routing and named-profile configuration."""

    configuration_version: str = "2"
    default_profile: str = "generic"
    profiles: Mapping[str, ChunkingProfile] = field(default_factory=default_chunking_profiles)
    routes: tuple[ChunkRoute, ...] = field(
        default_factory=lambda: (
            ChunkRoute(source_kind="jira", profile="jira"),
            ChunkRoute(source_kind="confluence", profile="confluence"),
            ChunkRoute(content_category="source_code", profile="code"),
            ChunkRoute(content_category="structured_data", profile="json"),
            ChunkRoute(content_category="document", profile="document"),
            ChunkRoute(content_category="table", profile="document"),
        )
    )

    def __post_init__(self) -> None:
        profiles = dict(self.profiles)
        if not self.configuration_version.strip():
            raise ValueError("configuration_version must be non-empty")
        if self.default_profile not in profiles:
            raise ValueError("default_profile must name a configured profile")
        for key, profile in profiles.items():
            if key != profile.name:
                raise ValueError(f"profile key/name mismatch: {key!r} != {profile.name!r}")
        for route in self.routes:
            if route.profile not in profiles:
                raise ValueError(f"route references unknown profile: {route.profile}")
        object.__setattr__(self, "profiles", MappingProxyType(profiles))
