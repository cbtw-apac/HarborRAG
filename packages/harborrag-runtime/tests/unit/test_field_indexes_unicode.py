"""Declared facets index the same ``fields.*`` key the Jira connector writes."""

from __future__ import annotations

from harborrag_adapters.connectors.jira.content import (
    custom_field_metadata,
    filterable_fields,
)
from harborrag_runtime.config.graph_build import GraphBuildConfig
from harborrag_runtime.ingestion.field_indexes import declared_field_indexes


def _config(*fields: str) -> GraphBuildConfig:
    return GraphBuildConfig.model_validate(
        {
            "tenants": [
                {
                    "tenant_id": "VN",
                    "sources": [
                        {
                            "source_scope_id": "VN",
                            "facets": [
                                {"name": f"facet_{index}", "field": field}
                                for index, field in enumerate(fields)
                            ],
                        }
                    ],
                }
            ]
        },
        strict=False,
    )


def test_an_accented_facet_indexes_the_key_evidence_carries():
    issue = {
        "fields": {"customfield_1": {"value": "Python"}, "customfield_2": {"value": "Kế toán"}},
        "names": {"customfield_1": "Kỹ năng", "customfield_2": "Đơn vị"},
        "schema": {"customfield_1": {"type": "option"}, "customfield_2": {"type": "option"}},
    }
    payload_keys = set(filterable_fields(custom_field_metadata(issue)))

    declared = declared_field_indexes(_config("Kỹ năng", "Đơn vị"))

    paths = {item.path for item in declared["VN"]}
    assert paths == {"fields.ky_nang", "fields.don_vi"}
    assert paths == {f"fields.{key}" for key in payload_keys}


def test_a_cjk_facet_must_be_declared_by_its_field_id():
    declared = declared_field_indexes(_config("経験年数", "customfield_500"))

    assert [item.path for item in declared["VN"]] == ["fields.customfield_500"]
