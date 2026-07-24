"""ParseInput path coercion hardening tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.blackbox


def test_string_input_is_never_read_as_a_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")

    parse_input = ParseInput.coerce(str(secret))

    assert parse_input.content == str(secret)
    assert parse_input.path is None
    assert "TOP SECRET" not in parse_input.read_text()


def test_path_strings_only_read_when_explicitly_opted_in(tmp_path: Path) -> None:
    target = tmp_path / "doc.txt"
    target.write_text("real body", encoding="utf-8")
    parse_input = ParseInput.coerce(str(target), allow_path_strings=True)
    assert parse_input.path == target
