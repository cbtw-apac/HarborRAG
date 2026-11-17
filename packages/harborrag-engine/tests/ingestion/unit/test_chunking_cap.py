from __future__ import annotations

import pytest

from harborrag_engine.ingestion.chunking.config import (
    ChunkingConfig,
    ChunkingLimits,
    ChunkingProfile,
    cap_chunking_config,
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
