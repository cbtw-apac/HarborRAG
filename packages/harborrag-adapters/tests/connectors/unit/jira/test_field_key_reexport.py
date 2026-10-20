from __future__ import annotations

from harborrag_adapters.connectors.jira.content import field_key as v1
from harborrag_core.sources import field_key


def test_field_key_matches_v1_jira() -> None:
    assert v1 is field_key
