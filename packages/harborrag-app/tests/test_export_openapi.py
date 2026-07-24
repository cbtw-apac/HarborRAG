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
    assert schema["info"]["version"] == "1.0.0-draft"
    paths = schema["paths"]
    assert {"/api/v1/health", "/api/v1/readyz", "/api/v1/diagnostics"} <= set(paths)
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert rendered == export_openapi()
