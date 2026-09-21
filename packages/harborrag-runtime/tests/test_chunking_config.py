"""The repository chunking policy must stay identical to the historical defaults."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from textwrap import dedent

import pytest

from harborrag_runtime.config import (
    ChunkingConfigurationError,
    load_chunking_config,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.chunking_profile import (
    chunk_strategy_fingerprint,
    default_chunking_config,
    resolve_chunking_config,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REPOSITORY_CONFIG = REPO_ROOT / "config" / "chunking.yaml"

# Pinned so that editing config/chunking.yaml surfaces as a deliberate change:
# this fingerprint is part of the processing identity, and any drift re-chunks
# every already-ingested document.
EXPECTED_FINGERPRINT = (
    "chunking-v3-b16a744e0e7485eaf75a0dbe08d856379ae2214dd4607005b778c1eb8c4a7c0c"
)


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "chunking.yaml"
    config_path.write_text(dedent(content), encoding="utf-8")
    return config_path


def test_repository_config_reproduces_the_hardcoded_defaults() -> None:
    loaded = load_chunking_config(REPOSITORY_CONFIG)
    expected = default_chunking_config()

    assert loaded.configuration_version == expected.configuration_version
    assert loaded.default_profile == expected.default_profile
    assert loaded.create_route_chunks == expected.create_route_chunks
    assert dict(loaded.source_profiles) == dict(expected.source_profiles)
    assert {key: asdict(profile) for key, profile in loaded.profiles.items()} == {
        key: asdict(profile) for key, profile in expected.profiles.items()
    }


def test_repository_config_preserves_the_processing_identity() -> None:
    loaded = chunk_strategy_fingerprint(load_chunking_config(REPOSITORY_CONFIG))

    assert loaded == chunk_strategy_fingerprint(default_chunking_config())
    assert loaded == EXPECTED_FINGERPRINT


def test_every_shipped_profile_keeps_a_distinct_soft_ceiling() -> None:
    """The middle packing tier must stay live.

    `ChunkingLimits` silently collapses an omitted soft ceiling onto the hard
    maximum, which is how the three-tier design went unused before.
    """

    config = load_chunking_config(REPOSITORY_CONFIG)

    for name, profile in config.profiles.items():
        assert profile.target_tokens < profile.soft_maximum_tokens < profile.maximum_tokens, name


def test_resolution_uses_the_repository_file_when_present() -> None:
    settings = RuntimeSettings(chunking_config_path=REPOSITORY_CONFIG)

    assert chunk_strategy_fingerprint(resolve_chunking_config(settings)) == EXPECTED_FINGERPRINT


def test_resolution_reads_the_configured_file_rather_than_the_defaults(tmp_path: Path) -> None:
    # A no-op loader would satisfy the assertions above by falling back, so
    # prove the file drives the result: a changed budget must move the
    # fingerprint away from the built-in one.
    config_path = _write_config(
        tmp_path,
        """
        default_profile: canonical
        profiles:
          canonical:
            strategy: canonical
            limits:
              minimum_tokens: 120
              target_tokens: 650
              maximum_tokens: 1100
              overlap_tokens: 0
        """,
    )
    settings = RuntimeSettings(chunking_config_path=config_path)
    resolved = resolve_chunking_config(settings)

    assert resolved.profiles["canonical"].target_tokens == 650
    assert chunk_strategy_fingerprint(resolved) != EXPECTED_FINGERPRINT


def test_resolution_falls_back_to_defaults_when_the_file_is_absent(tmp_path: Path) -> None:
    settings = RuntimeSettings(chunking_config_path=tmp_path / "absent.yaml")

    assert chunk_strategy_fingerprint(resolve_chunking_config(settings)) == EXPECTED_FINGERPRINT


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        default_profile: canonical
        unexpected: 1
        profiles:
          canonical:
            strategy: canonical
        """,
    )

    with pytest.raises(ChunkingConfigurationError, match="unexpected"):
        load_chunking_config(config_path)


def test_duplicate_profile_keys_are_rejected(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        default_profile: canonical
        profiles:
          canonical:
            strategy: canonical
          canonical:
            strategy: jira
        """,
    )

    with pytest.raises(ChunkingConfigurationError, match="duplicate key"):
        load_chunking_config(config_path)


def test_token_limits_must_be_ordered(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        default_profile: canonical
        profiles:
          canonical:
            strategy: canonical
            limits:
              minimum_tokens: 900
              target_tokens: 700
              maximum_tokens: 1100
              overlap_tokens: 0
        """,
    )

    with pytest.raises(ChunkingConfigurationError, match="minimum_tokens"):
        load_chunking_config(config_path)


def test_default_profile_must_be_configured(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        default_profile: missing
        profiles:
          canonical:
            strategy: canonical
        """,
    )

    with pytest.raises(ChunkingConfigurationError, match="default_profile"):
        load_chunking_config(config_path)


def test_source_mapping_must_reference_configured_profiles(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        default_profile: canonical
        profiles:
          canonical:
            strategy: canonical
        source_profiles:
          jira: jira
        """,
    )

    with pytest.raises(ChunkingConfigurationError, match="unknown profiles"):
        load_chunking_config(config_path)
