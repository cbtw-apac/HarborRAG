"""The structured-output schemas must be accepted by OpenAI strict mode.

OpenAI rejects a ``response_format`` json_schema whose object schemas do not
declare ``additionalProperties: false``, with HTTP 400. Every structured
memory call went out without it, so extraction never once succeeded and
recall had nothing to return -- the failure was invisible because both call
sites swallow it.

The declaration has to be schema-only: these models are deliberately
permissive at parse time, so an unexpected key must still be dropped rather
than invalidate the whole response.
"""

from __future__ import annotations

import pytest

from harborrag_memory.context.condenser import CondenseRequest
from harborrag_memory.context.facts import ProposedFact, ProposedFacts

STRUCTURED_MODELS = [ProposedFacts, ProposedFact, CondenseRequest]


def _objects(node: object, path: str = "root") -> list[tuple[str, dict]]:
    """Every object schema inside a JSON schema, with the schema itself."""

    found: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append((path, node))
        for key, value in node.items():
            found.extend(_objects(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_objects(value, f"{path}[{index}]"))
    return found


@pytest.mark.parametrize("model", STRUCTURED_MODELS, ids=lambda m: m.__name__)
def test_structured_schema_requires_every_property(model: type) -> None:
    """OpenAI strict mode also demands ``required`` list every property.

    Pydantic emits ``required`` only for fields without defaults, and every
    field on these models is deliberately defaulted -- so ``required`` came
    out empty and the provider rejected the request just as it did for the
    missing ``additionalProperties``.
    """

    offenders = [
        (path, sorted(set(schema.get("properties", {})) - set(schema.get("required", []))))
        for path, schema in _objects(model.model_json_schema())
        if set(schema.get("properties", {})) - set(schema.get("required", []))
    ]

    assert offenders == [], f"{model.__name__} would be rejected by OpenAI strict mode"


def _object_schemas(node: object, path: str = "root") -> list[tuple[str, object]]:
    """Every object schema inside a JSON schema, with its additionalProperties."""

    found: list[tuple[str, object]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append((path, node.get("additionalProperties", "MISSING")))
        for key, value in node.items():
            found.extend(_object_schemas(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_object_schemas(value, f"{path}[{index}]"))
    return found


@pytest.mark.parametrize("model", STRUCTURED_MODELS, ids=lambda m: m.__name__)
def test_structured_schema_forbids_additional_properties(model: type) -> None:
    """Every object in the emitted schema, nested ones included, declares it."""

    offenders = [
        (path, value)
        for path, value in _object_schemas(model.model_json_schema())
        if value is not False
    ]

    assert offenders == [], f"{model.__name__} would be rejected by OpenAI strict mode"


def test_proposed_facts_still_drops_unexpected_keys() -> None:
    """Schema-only: parsing stays permissive, as the docstring promises."""

    parsed = ProposedFacts.model_validate(
        {"facts": [{"content": "Teal is preferred.", "unexpected": 1}], "extra": True}
    )

    assert [fact.content for fact in parsed.facts] == ["Teal is preferred."]


def test_condense_request_still_drops_unexpected_keys() -> None:
    parsed = CondenseRequest.model_validate({"query": "What colour?", "unexpected": 1})

    assert parsed.query == "What colour?"
