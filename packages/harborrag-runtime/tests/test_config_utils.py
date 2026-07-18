from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest
from harborrag_runtime.config.errors import ConfigurationError
from harborrag_runtime.config.utils import parse_environment_references


def test_parse_environment_references_rejects_key_whitespace() -> None:
    with pytest.raises(ConfigurationError, match="surrounding whitespace"):
        parse_environment_references(
            {" token": "TOKEN_ENV_VAR"},
            label="test",
        )


def test_parse_environment_references_accepts_clean_keys() -> None:
    parsed = parse_environment_references(
        {"token": "TOKEN_ENV_VAR"},
        label="test",
    )

    assert parsed == {"token": "TOKEN_ENV_VAR"}


class _DuplicateItemsMapping(Mapping[str, str]):
    """A mapping stand-in whose ``items()`` yields duplicate keys.

    A real ``dict``/YAML mapping cannot hold two entries with the exact same
    key (PyYAML silently keeps only the last), so this stands in for a
    mapping implementation that could otherwise slip a duplicate normalized
    target past the single-pass loop.
    """

    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._items = items

    def __getitem__(self, key: str) -> str:
        for stored_key, value in self._items:
            if stored_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter({key for key, _ in self._items})

    def __len__(self) -> int:
        return len({key for key, _ in self._items})

    def items(self):  # type: ignore[override]
        return self._items


def test_parse_environment_references_rejects_duplicate_normalized_target() -> None:
    duplicate = _DuplicateItemsMapping([("token", "TOKEN_A"), ("token", "TOKEN_B")])

    with pytest.raises(ConfigurationError, match="more than once"):
        parse_environment_references(duplicate, label="test")
