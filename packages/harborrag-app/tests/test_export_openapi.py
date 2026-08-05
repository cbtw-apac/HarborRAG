"""The exported OpenAPI schema is valid, stable JSON with the M0 surface (ST10)."""

from __future__ import annotations

import json

import pytest

from harborrag_app.api.export_openapi import export_openapi


@pytest.mark.blackbox
def test_export_produces_stable_schema_with_m0_surface() -> None:
    """Schema parses, carries the title/version, the M0 routes, and the
    bearer security scheme; two exports are byte-identical (diffable in CI)."""
    rendered = export_openapi()
    schema = json.loads(rendered)
    assert schema["info"]["title"] == "HarborRAG Control Plane API"
    assert schema["info"]["version"] == "0.1.0"
    paths = schema["paths"]
    assert {
        "/api/v1/metrics",
        "/api/v1/health",
        "/v1/ingestions",
        "/v1/ingestions/{task_id}",
        "/v1/ingestions/{task_id}/documents",
        "/v1/ingestions/{task_id}/cancel",
        "/v1/ingestions/{task_id}/retry-failures",
        "/v1/chat/completions",
        "/v1/chat/sessions",
        "/v1/agent/completions",
        "/v1/agent/sessions",
        "/v1/retrieval/vector",
        "/v1/retrieval/graph/triplets",
        "/v1/retrieval/graph/paths",
        "/v1/retrieval/graph/subgraphs",
        "/v1/admin/projections/{tenant}",
    } <= set(paths)
    assert set(paths["/v1/chat/completions"]) >= {"get"}
    assert "post" not in paths["/v1/chat/completions"]
    assert set(paths["/v1/agent/completions"]) >= {"get"}
    assert "/v1/retrieval/search" not in paths
    assert "/api/v1/readyz" not in paths
    assert "/api/v1/diagnostics" not in paths
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert rendered == export_openapi()
