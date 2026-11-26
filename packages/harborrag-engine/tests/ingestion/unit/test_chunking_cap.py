from __future__ import annotations

import pytest

from harborrag_engine.ingestion.chunking.config import (
    ChunkingConfig,
    ChunkingLimits,
    ChunkingProfile,
    cap_chunking_config,
    pin_chunking_overlap,
)

pytestmark = pytest.mark.unit

MINIMUM_CAP = 64
MAXIMUM_CAP = 4096


def test_cap_at_or_above_every_maximum_returns_same_config() -> None:
    config = ChunkingConfig()

    assert cap_chunking_config(config, 1200) is config


def test_cap_lowers_every_limit_in_order() -> None:
    capped = cap_chunking_config(ChunkingConfig(), 256)

    for profile in capped.profiles.values():
        limits = profile.limits
        assert limits.maximum_tokens == 256
        assert limits.minimum_tokens <= limits.target_tokens <= limits.soft_maximum_tokens
        assert limits.soft_maximum_tokens <= limits.maximum_tokens
        assert limits.overlap_tokens < limits.target_tokens


def test_cap_at_minimum_64() -> None:
    config = ChunkingConfig(
        profiles={
            "canonical": ChunkingProfile(
                name="canonical", strategy="canonical", limits=ChunkingLimits(80, 600, 1000, 40)
            )
        },
        source_profiles={},
    )

    limits = cap_chunking_config(config, 64).profiles["canonical"].limits

    assert (limits.minimum_tokens, limits.target_tokens) == (64, 64)
    assert (limits.soft_maximum_tokens, limits.maximum_tokens, limits.overlap_tokens) == (
        64,
        64,
        40,
    )


def test_cap_keeps_profile_at_or_below_cap_untouched() -> None:
    config = ChunkingConfig()

    capped = cap_chunking_config(config, 1100)

    assert capped.profiles["canonical"] is config.profiles["canonical"]
    assert capped.profiles["jira"] is config.profiles["jira"]
    assert capped.profiles["confluence"].limits.maximum_tokens == 1100


def test_cap_yields_valid_limits_for_every_cap_in_range() -> None:
    config = ChunkingConfig()

    for cap in range(MINIMUM_CAP, MAXIMUM_CAP + 1):
        for profile in cap_chunking_config(config, cap).profiles.values():
            assert profile.limits.maximum_tokens <= cap


def test_pin_overlap_sets_every_profile_to_the_pinned_value() -> None:
    pinned = pin_chunking_overlap(ChunkingConfig(), 64)

    for profile in pinned.profiles.values():
        assert profile.overlap_tokens == 64


def test_pin_overlap_clamps_below_a_profile_target_instead_of_rejecting() -> None:
    config = ChunkingConfig(
        profiles={
            "canonical": ChunkingProfile(
                name="canonical", strategy="canonical", limits=ChunkingLimits(80, 100, 1000, 0)
            )
        },
        source_profiles={},
    )

    limits = pin_chunking_overlap(config, 512).profiles["canonical"].limits

    assert limits.overlap_tokens == 99


def test_pin_overlap_leaves_profiles_already_at_the_pinned_value_untouched() -> None:
    config = ChunkingConfig()
    already_zero = {
        name for name, profile in config.profiles.items() if profile.overlap_tokens == 0
    }

    pinned = pin_chunking_overlap(config, 0)

    for name in already_zero:
        assert pinned.profiles[name] is config.profiles[name]


def test_pin_overlap_composes_with_a_lower_max_tokens_cap() -> None:
    capped = cap_chunking_config(ChunkingConfig(), 150)
    pinned = pin_chunking_overlap(capped, 500)

    for profile in pinned.profiles.values():
        limits = profile.limits
        assert limits.maximum_tokens == 150
        assert limits.overlap_tokens == limits.target_tokens - 1
