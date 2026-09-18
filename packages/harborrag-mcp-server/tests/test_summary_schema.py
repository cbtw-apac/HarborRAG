"""Nested summary payloads remain strict inside the graph navigation contract."""

import pytest
from jsonschema import Draft202012Validator, ValidationError

from harborrag_core.summary_cards import SummaryCard, SummaryView
from harborrag_engine.tools.output_schemas import NODE_SCHEMA


def test_summary_card_is_validated_inside_node_schema():
    validator = Draft202012Validator(NODE_SCHEMA)
    node = {
        "node_key": "source",
        "node_kind": "DataSource",
        "entity_type": "data_source",
        "summary": SummaryView(
            status="current", card=SummaryCard(description="A source.")
        ).model_dump(mode="json", exclude_none=True),
    }
    validator.validate(node)
    node["summary"]["card"]["unexpected"] = "secret"
    with pytest.raises(ValidationError):
        validator.validate(node)
