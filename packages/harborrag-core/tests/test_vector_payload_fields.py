from __future__ import annotations

import logging

import pytest

from harborrag_core.ingestion import VectorPayload

pytestmark = pytest.mark.unit

_LONG_FIELD_NAME = "Estimated engineering effort for the platform migration " * 3


def _payload(fields: dict[str, object]) -> VectorPayload:
    return VectorPayload.model_validate(
        {
            "chunk_id": "chunk-1",
            "logical_chunk_id": "logical-1",
            "document_id": "document-1",
            "document_version_id": "version-1",
            "record_kind": "evidence",
            "chunk_kind": ("jira_field",),
            "connector_type": "jira",
            "source_scope_id": "DEMO",
            "content": "DEMO-1: Migrate the build agents",
            "token_count": 6,
            "content_hash": "hash-1",
            "citation_locator": {"source_element_ids": ["summary"]},
            "quality_score": 1.0,
            "fields": fields,
        }
    )


def _jira_style_key(name: str) -> str:
    return "_".join(name.casefold().split())


def test_a_field_key_longer_than_128_characters_is_left_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    long_key = _jira_style_key(_LONG_FIELD_NAME)
    assert len(long_key) > 128

    with caplog.at_level(logging.WARNING, logger="harborrag.core.ingestion.projection_vector"):
        payload = _payload({long_key: "High", "team": "Platform"})

    assert payload.fields == {"team": "Platform"}
    assert "document_version_id=version-1 chunk_id=chunk-1" in caplog.text
    assert "fields=2 unnormalized_keys=1 over_limit=0" in caplog.text
    assert long_key not in caplog.text


def test_an_unnormalized_field_key_is_left_out() -> None:
    assert _payload({"Skill Set": "Data", "skill_set": "Data"}).fields == {"skill_set": "Data"}


def test_only_the_first_128_fields_in_key_order_are_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fields: dict[str, object] = {f"field_{index:03d}": f"value {index}" for index in range(140)}
    reversed_fields = dict(reversed(fields.items()))

    with caplog.at_level(logging.WARNING, logger="harborrag.core.ingestion.projection_vector"):
        payload = _payload(reversed_fields)

    assert payload.fields is not None
    assert sorted(payload.fields) == [f"field_{index:03d}" for index in range(128)]
    assert _payload(fields).fields == payload.fields
    assert "fields=140 unnormalized_keys=0 over_limit=12" in caplog.text
    assert "field_139" not in caplog.text


def test_a_payload_whose_every_field_is_left_out_carries_no_fields() -> None:
    assert _payload({"Skill Set": "Data"}).fields is None


def test_128_normalized_fields_are_kept_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fields: dict[str, object] = {f"field_{index:03d}": index for index in range(128)}

    with caplog.at_level(logging.WARNING, logger="harborrag.core.ingestion.projection_vector"):
        payload = _payload(fields)

    assert payload.fields is not None
    assert len(payload.fields) == 128
    assert caplog.text == ""
