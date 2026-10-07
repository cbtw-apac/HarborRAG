"""Every reader tool advertises defaults its own schema accepts."""

from __future__ import annotations

import pytest

from harborrag_engine.tools.base import BaseTool
from harborrag_engine.tools.catalog import build_reader_tool_catalog
from harborrag_engine.tools.references import KnowledgeReferenceStore

_CATALOG = build_reader_tool_catalog(None, KnowledgeReferenceStore())


@pytest.mark.parametrize("tool", _CATALOG, ids=lambda tool: tool.spec.name)
def test_numeric_defaults_are_within_their_bounds(tool: BaseTool) -> None:
    properties = tool.spec.input_schema.get("properties", {})
    for field_name, schema in properties.items():
        if not isinstance(schema, dict) or not isinstance(schema.get("default"), (int, float)):
            continue
        default = schema["default"]
        assert default <= schema.get("maximum", default), field_name
        assert default >= schema.get("minimum", default), field_name
