"""MCP reader-key helpers: format, hashing and comparison."""

from __future__ import annotations

import pytest

from harborrag_core.security.api_keys import (
    KEY_ID_LENGTH,
    generate_key,
    hash_key,
    hash_matches,
    parse_key,
)


def test_a_generated_key_parses_and_verifies() -> None:
    key = generate_key("prod")

    parsed = parse_key(key.raw_key)
    assert parsed is not None
    assert parsed.environment == "prod"
    assert parsed.key_id == key.key_id
    assert len(key.key_id) == KEY_ID_LENGTH
    assert hash_matches(key.raw_key, key.secret_hash)
    assert hash_matches(key.raw_key, key.secret_hash.upper())  # stored case is irrelevant


def test_one_changed_secret_character_fails_verification() -> None:
    key = generate_key("dev")
    head, secret = key.raw_key.rsplit(".", 1)
    flipped = ("A" if secret[0] != "A" else "B") + secret[1:]

    assert not hash_matches(f"{head}.{flipped}", key.secret_hash)


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "hrk_prod_v1_xyz.abc",
        "hrk_staging_v1_" + "0" * 24 + "." + "a" * 43,  # unknown environment
        "hrk_prod_v2_" + "0" * 24 + "." + "a" * 43,  # unknown format version
        "hrk_prod_v1_" + "0" * 24 + "." + "a" * 42,  # secret too short
        "x" * 500,
        "hrk_prod_v1_" + "0" * 24 + ".é" + "a" * 42,  # non-ASCII
    ],
)
def test_malformed_keys_do_not_parse(candidate: str) -> None:
    assert parse_key(candidate) is None


def test_the_stored_hash_is_not_itself_a_key() -> None:
    key = generate_key("dev")

    assert parse_key(key.secret_hash) is None
    assert parse_key(hash_key(key.raw_key)) is None


def test_repr_never_shows_the_secret_or_hash() -> None:
    key = generate_key("dev")

    text = repr(key)
    assert key.key_id in text
    assert key.raw_key not in text
    assert key.secret_hash not in text


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(ValueError, match="environment"):
        generate_key("test")


def test_keys_are_unique() -> None:
    first, second = generate_key("dev"), generate_key("dev")

    assert first.key_id != second.key_id
    assert first.raw_key != second.raw_key
