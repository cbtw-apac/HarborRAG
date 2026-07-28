"""Rejection and normalisation paths for the chunking config/schema guards.

These frozen dataclasses normalise their own inputs in ``__post_init__`` and
reject inconsistent ones. The chunking service suites cover the happy paths;
this module pins the guards and the in-place normalisation they perform.
"""

from __future__ import annotations

import pytest

from harborrag_engine.ingestion.chunking.config import (
    ChunkingConfig,
    ChunkingLimits,
    ChunkingProfile,
    ChunkRoute,
)
from harborrag_engine.ingestion.chunking.schemas import ChunkReference

# --------------------------------------------------------------------------
# ChunkingLimits
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"minimum_tokens": -1}, "minimum_tokens must not be negative"),
        ({"target_tokens": 0}, "target_tokens must be positive"),
        (
            {"minimum_tokens": 800, "target_tokens": 700},
            "minimum_tokens must not exceed target_tokens",
        ),
        (
            {"maximum_tokens": 500, "target_tokens": 700},
            "maximum_tokens must not be below target_tokens",
        ),
        ({"overlap_tokens": -1}, "overlap_tokens must not be negative"),
        (
            {"overlap_tokens": 700, "target_tokens": 700},
            "overlap_tokens must be below target_tokens",
        ),
    ],
)
def test_chunking_limits_reject_inconsistent_bounds(
    changes: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ChunkingLimits(**changes)


def test_chunking_limits_accept_the_documented_defaults() -> None:
    limits = ChunkingLimits()

    assert limits.minimum_tokens <= limits.target_tokens <= limits.maximum_tokens
    assert limits.overlap_tokens < limits.target_tokens


def test_chunking_limits_allow_equal_minimum_and_target() -> None:
    limits = ChunkingLimits(minimum_tokens=700, target_tokens=700, maximum_tokens=700)

    assert limits.maximum_tokens == 700


# --------------------------------------------------------------------------
# ChunkingProfile
# --------------------------------------------------------------------------


def test_profile_requires_a_name_and_strategy() -> None:
    with pytest.raises(ValueError, match="profile name must be non-empty"):
        ChunkingProfile(name="  ", strategy="document")
    with pytest.raises(ValueError, match="profile strategy must be non-empty"):
        ChunkingProfile(name="document", strategy="   ")


def test_profile_strips_its_identity_and_exposes_limits() -> None:
    profile = ChunkingProfile(name="  document  ", strategy="  structural  ")

    assert profile.name == "document"
    assert profile.strategy == "structural"
    assert profile.minimum_tokens == profile.limits.minimum_tokens
    assert profile.target_tokens == profile.limits.target_tokens


# --------------------------------------------------------------------------
# ChunkRoute
# --------------------------------------------------------------------------


def test_route_requires_a_profile_and_a_condition() -> None:
    with pytest.raises(ValueError, match="route profile must be non-empty"):
        ChunkRoute(profile="   ", source_kind="jira")
    with pytest.raises(ValueError, match="at least one match condition"):
        ChunkRoute(profile="document")


def test_route_normalises_its_conditions() -> None:
    route = ChunkRoute(
        profile="  document  ",
        source_kind="  Confluence  ",
        content_type="  Text/HTML; charset=utf-8  ",
        content_category="  Document  ",
    )

    assert route.profile == "document"
    assert route.source_kind == "confluence"
    # The media-type parameter is dropped so routing keys stay comparable.
    assert route.content_type == "text/html"
    assert route.content_category == "document"


def test_route_matching_treats_unset_conditions_as_wildcards() -> None:
    route = ChunkRoute(profile="document", source_kind="confluence")

    assert route.matches(
        source_kind="confluence",
        content_type="text/html",
        content_category="document",
    )
    assert not route.matches(
        source_kind="jira",
        content_type="text/html",
        content_category="document",
    )


def test_route_matching_requires_every_configured_condition() -> None:
    route = ChunkRoute(
        profile="document",
        source_kind="confluence",
        content_category="document",
    )

    assert not route.matches(
        source_kind="confluence",
        content_type="text/html",
        content_category="table",
    )


# --------------------------------------------------------------------------
# ChunkingConfig
# --------------------------------------------------------------------------


def test_config_requires_a_version_and_a_known_default_profile() -> None:
    with pytest.raises(ValueError, match="configuration_version must be non-empty"):
        ChunkingConfig(configuration_version="  ")
    with pytest.raises(ValueError, match="default_profile must name a configured profile"):
        ChunkingConfig(default_profile="nonexistent")


def test_config_rejects_a_profile_key_name_mismatch() -> None:
    profile = ChunkingProfile(name="document", strategy="structural")

    with pytest.raises(ValueError, match="profile key/name mismatch"):
        ChunkingConfig(
            default_profile="mislabelled",
            profiles={"mislabelled": profile},
            routes=(),
        )


def test_config_rejects_a_route_to_an_unknown_profile() -> None:
    profile = ChunkingProfile(name="document", strategy="structural")

    with pytest.raises(ValueError, match="route references unknown profile: missing"):
        ChunkingConfig(
            default_profile="document",
            profiles={"document": profile},
            routes=(ChunkRoute(profile="missing", source_kind="jira"),),
        )


def test_config_freezes_its_profile_mapping() -> None:
    profile = ChunkingProfile(name="document", strategy="structural")
    config = ChunkingConfig(
        default_profile="document",
        profiles={"document": profile},
        routes=(),
    )

    with pytest.raises(TypeError):
        config.profiles["document"] = profile  # type: ignore[index]


def test_config_defaults_are_internally_consistent() -> None:
    config = ChunkingConfig()

    assert config.default_profile in config.profiles
    for route in config.routes:
        assert route.profile in config.profiles


# --------------------------------------------------------------------------
# ChunkReference
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"logical_chunk_id": ""}, "identity values must be non-empty"),
        ({"chunk_revision_id": ""}, "identity values must be non-empty"),
        ({"ordinal": -1}, "ordinal must be non-negative"),
        ({"token_count": 0}, "ordinal must be non-negative"),
        ({"content_hash": ""}, "content_hash must be non-empty"),
    ],
)
def test_chunk_reference_rejects_invalid_identity(
    changes: dict[str, object],
    message: str,
) -> None:
    fields: dict[str, object] = {
        "logical_chunk_id": "logical-1",
        "chunk_revision_id": "rev-1",
        "ordinal": 0,
        "content_hash": "hash-1",
        "token_count": 4,
    }
    fields.update(changes)

    with pytest.raises(ValueError, match=message):
        ChunkReference(**fields)  # type: ignore[arg-type]


def test_chunk_reference_body_uri_is_optional() -> None:
    reference = ChunkReference(
        logical_chunk_id="logical-1",
        chunk_revision_id="rev-1",
        ordinal=0,
        content_hash="hash-1",
        token_count=4,
    )

    assert reference.body_uri is None
