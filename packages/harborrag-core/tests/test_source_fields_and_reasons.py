from __future__ import annotations

import pytest

from harborrag_core.sources import DocKey, ItemFailure, SkipReason, field_key

_KEY = DocKey("s", "doc", "1")


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Skill Set", "skill_set"),
        ("  Years of Exp. ", "years_of_exp"),
        ("customfield_10042", "customfield_10042"),
        ("Ünïcode Name", "n_code_name"),
        ("---", ""),
    ],
)
def test_field_key(name: str, key: str) -> None:
    assert field_key(name) == key


def test_skipped_failure_needs_a_skip_reason() -> None:
    ItemFailure(SkipReason.TOO_LARGE, key=_KEY, skipped=True)
    ItemFailure("download_failed", key=_KEY)
    with pytest.raises(ValueError, match="skip reason"):
        ItemFailure("big", key=_KEY, skipped=True)
