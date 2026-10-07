"""API key format: generate, parse, hash, and never leak the secret"""

from __future__ import annotations

import pytest

from harborrag_core.security import api_keys
from harborrag_core.security.api_keys import (
    ParsedKey,
    generate_key,
    hash_matches,
    parse_key,
)

_VALID_ID = "0123456789abcdef01234567"  # 24 lowercase hex
_VALID_SECRET = "A" * 43  # 43 url-safe chars


def _key(
    prefix: str = "hrk",
    env: str = "prod",
    version: str = "v1",
    key_id: str = _VALID_ID,
    secret: str = _VALID_SECRET,
) -> str:
    return f"{prefix}_{env}_{version}_{key_id}.{secret}"


def test_baseline_key_is_valid() -> None:
    assert parse_key(_key()) == ParsedKey("prod", _VALID_ID)


# Invalid prefix and format
@pytest.mark.parametrize(
    "raw_key",
    [
        _key(prefix="xrk"),
        _key(prefix="HRK"),
        _key(env="test"),
        _key(env="Prod"),
        _key(version="v2"),
        f"hrk_prod_v1_{_VALID_ID}_{_VALID_SECRET}",
        _key() + "\n",
        " " + _key(),
    ],
    ids=[
        "bad-prefix",
        "uppercase-prefix",
        "unknown-env",
        "uppercase-env",
        "bad-version",
        "missing-dot",
        "trailing-newline",
        "leading-space",
    ],
)
def test_parse_key_rejects_bad_prefix_or_format(raw_key: str) -> None:
    assert parse_key(raw_key) is None


# Wrong length
@pytest.mark.parametrize(
    "raw_key",
    [
        _key(key_id=_VALID_ID[:-1]),
        _key(key_id=_VALID_ID + "0"),
        _key(key_id=_VALID_ID.upper()),
        _key(secret=_VALID_SECRET[:-1]),
        _key(secret=_VALID_SECRET + "A"),
        _key(secret="A" * 42 + "="),
    ],
    ids=["id-23", "id-25", "id-uppercase-hex", "secret-42", "secret-44", "secret-padding"],
)
def test_parse_key_rejects_wrong_part_lengths(raw_key: str) -> None:
    assert parse_key(raw_key) is None


# Non-ASCII in a slot that is otherwise valid
@pytest.mark.parametrize(
    "raw_key",
    [
        _key(key_id=_VALID_ID[:-1] + "é"),
        _key(key_id=_VALID_ID[:-1] + "٣"),  # Arabic-Indic digit; \d would accept it
        _key(key_id=_VALID_ID[:-1] + "０"),  # fullwidth zero
        _key(secret=_VALID_SECRET[:-1] + "é"),
    ],
    ids=["id-accent", "id-unicode-digit", "id-fullwidth", "secret-accent"],
)
def test_parse_key_rejects_non_ascii(raw_key: str) -> None:
    assert parse_key(raw_key) is None


# Prove the 120-character guard actually runs
def test_parse_key_rejects_oversized_input_before_regex(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ExplodingRegex:
        def fullmatch(self, _: str) -> None:
            raise AssertionError("regex must not run on oversized input")

    monkeypatch.setattr(api_keys, "_KEY_RE", _ExplodingRegex())

    assert parse_key("hrk_prod_v1_" + "a" * 200) is None


def test_parse_key_accepts_input_at_length_limit_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _RecordingRegex:
        def fullmatch(self, s: str) -> None:
            calls.append(s)

    monkeypatch.setattr(api_keys, "_KEY_RE", _RecordingRegex())
    parse_key("a" * 120)

    assert calls == ["a" * 120]  # exactly 120 still reaches the regex


def test_generated_key_parses_back_to_its_public_parts() -> None:
    key = generate_key("prod")

    assert parse_key(key.raw_key) == ParsedKey("prod", key.key_id)


def test_hash_matches_the_generated_key() -> None:
    key = generate_key("prod")

    assert hash_matches(key.raw_key, key.secret_hash) is True


def test_hash_rejects_key_with_one_secret_character_changed() -> None:
    key = generate_key("prod")
    last = key.raw_key[-1]
    tampered = key.raw_key[:-1] + ("A" if last != "A" else "B")

    assert parse_key(tampered) is not None
    assert hash_matches(tampered, key.secret_hash) is False


def test_stored_hash_is_not_accepted_as_a_key() -> None:
    key = generate_key("prod")

    assert parse_key(key.secret_hash) is None


@pytest.mark.parametrize(
    "raw_key",
    [
        "hrk_prod_v1_xyz.abc",
        "",
        "a" * 500,
        "hrk_prod_v1_" + "é" * 24 + "." + "x" * 43,
    ],
    ids=["malformed", "empty", "too-long", "non-ascii"],
)
def test_parse_key_rejects_invalid_input(raw_key: str) -> None:
    assert parse_key(raw_key) is None


def test_repr_does_not_expose_secret_or_hash() -> None:
    key = generate_key("dev")
    text = repr(key)

    assert key.raw_key not in text
    assert key.raw_key.split(".", 1)[1] not in text
    assert key.secret_hash not in text


def test_generate_key_rejects_unknown_environment() -> None:
    with pytest.raises(ValueError):
        generate_key("test")  # type: ignore[arg-type]


def test_two_generated_keys_are_distinct() -> None:
    first = generate_key("prod")
    second = generate_key("prod")

    assert first.key_id != second.key_id
    assert first.raw_key != second.raw_key
