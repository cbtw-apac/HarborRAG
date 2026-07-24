"""ParseInput path coercion hardening tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.parsers.input_loading import (
    coerce_parse_input,
    read_parse_input_text,
)

pytestmark = pytest.mark.blackbox


def test_string_input_is_never_read_as_a_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")

    parse_input = coerce_parse_input(str(secret))

    assert parse_input.content == str(secret)
    assert parse_input.path is None
    assert "TOP SECRET" not in read_parse_input_text(parse_input)


def test_path_strings_only_read_when_explicitly_opted_in(tmp_path: Path) -> None:
    target = tmp_path / "doc.txt"
    target.write_text("real body", encoding="utf-8")
    parse_input = coerce_parse_input(str(target), allow_path_strings=True)
    assert parse_input.path == target
