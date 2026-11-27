"""The ceilings enforced around one tool call."""

from __future__ import annotations

import pytest

from harborrag_engine.tools.base import ToolSpec
from harborrag_engine.tools.budgets import ToolBudget, result_count

pytestmark = pytest.mark.unit

_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["query"],
    "properties": {"query": {"type": "string", "maxLength": 8}},
    "additionalProperties": False,
}


def _spec(**overrides: object) -> ToolSpec:
    fields: dict[str, object] = {
        "name": "probe",
        "description": "a probe",
        "input_schema": _SCHEMA,
    }
    fields.update(overrides)
    return ToolSpec(**fields)  # type: ignore[arg-type]  # overrides hold ToolSpec fields


def test_an_ingestion_tool_is_refused_unless_it_is_switched_on() -> None:
    budget = ToolBudget(label="MCP")

    with pytest.raises(PermissionError, match="ingestion tools are disabled"):
        budget.check_call(_spec(capability="ingestion"), {"query": "x"})

    ToolBudget(label="MCP", allow_ingestion=True).check_call(
        _spec(capability="ingestion"), {"query": "x"}
    )


def test_a_payload_that_cannot_be_serialized_is_named_as_such() -> None:
    """Otherwise a bare TypeError escapes as an unexplained transport fault."""
    budget = ToolBudget(label="MCP")

    with pytest.raises(ValueError, match="MCP arguments must be JSON serializable"):
        budget.check_call(_spec(), {"query": object()})
    with pytest.raises(ValueError, match="MCP output must be JSON serializable"):
        budget.check_output({"results": object()})


def test_a_tool_whose_schema_is_invalid_is_our_bug_not_the_callers() -> None:
    budget = ToolBudget(label="MCP")
    broken = _spec(input_schema={"type": "not-a-type"})

    with pytest.raises(RuntimeError, match="invalid input schema"):
        budget.check_call(broken, {"query": "x"})
    with pytest.raises(RuntimeError, match="invalid output schema"):
        budget.check_output_schema({"ok": True}, {"type": "not-a-type"})
    with pytest.raises(RuntimeError, match="has no output schema"):
        budget.check_output_schema({"ok": True}, None)
    with pytest.raises(RuntimeError, match="returned an invalid output"):
        budget.check_output_schema({"ok": "yes"}, {"type": "object", "required": ["missing"]})


def test_detail_names_the_failing_field_only_when_asked() -> None:
    """The default reports the bare contract; a budget that opts in names the field."""
    quiet = ToolBudget(label="MCP")
    loud = ToolBudget(label="MCP", detail_in_errors=True)

    with pytest.raises(ValueError) as silent:
        quiet.check_call(_spec(), {"query": "far too long"})
    with pytest.raises(ValueError) as spoken:
        loud.check_call(_spec(), {"query": "far too long"})

    assert str(silent.value) == "MCP arguments do not match the tool schema."
    assert "query: " in str(spoken.value)


_MARKER = "synthetic-probe-marker"
_DETAILED_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["query", "lane"],
    "properties": {
        "query": {"type": "string", "maxLength": 8},
        "lane": {"type": "string", "enum": ["dense", "sparse", "hybrid"]},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
        "filters": {"type": "object", "additionalProperties": {"type": "string"}},
        "mode": {"anyOf": [{"const": "flat"}, {"const": "local_semantic"}]},
    },
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"query": [_MARKER], "lane": "dense"}, "query: must be of type string"),
        ({"query": _MARKER, "lane": "dense"}, "query: must be at most 8 characters long"),
        ({"query": "x", "lane": _MARKER}, 'lane: must be one of ["dense","sparse","hybrid"]'),
        ({"query": "x", "lane": "dense", "top_k": 21}, "top_k: must be at most 20"),
        ({"query": "x"}, "lane: is required"),
        (
            {"query": "x", "lane": "dense", _MARKER: 1},
            'arguments: allows only ["filters","lane","mode","query","top_k"]',
        ),
        ({"query": "x", "lane": "dense", "filters": {f"{_MARKER} text": 1}}, "filters.*: must be"),
        ({"query": "x", "lane": "dense", "mode": _MARKER}, "mode: does not satisfy anyOf"),
    ],
)
def test_detail_names_the_field_and_constraint_but_never_the_value(
    arguments: dict[str, object], expected: str
) -> None:
    """The refusal text reaches the client and the MCP library's span status in traces."""
    budget = ToolBudget(label="MCP", detail_in_errors=True)

    with pytest.raises(ValueError) as rejected:
        budget.check_call(_spec(input_schema=_DETAILED_SCHEMA), arguments)

    assert expected in str(rejected.value)
    assert _MARKER not in str(rejected.value)


def test_a_long_detail_is_truncated_rather_than_forwarded_whole() -> None:
    budget = ToolBudget(label="MCP", detail_in_errors=True)
    wide = _spec(
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "enum": ["option-" * 40] * 20}},
        }
    )

    with pytest.raises(ValueError) as rejected:
        budget.check_call(wide, {"query": "nope"})

    assert str(rejected.value).endswith("...")
    assert len(str(rejected.value)) < 400


def test_results_are_counted_from_whichever_list_a_tool_returns() -> None:
    assert result_count({"results": [1, 2, 3]}) == 3
    assert result_count({"memories": [1, 2]}) == 2
    assert result_count({"data": {"rows": [1, 2, 3, 4], "notes": [1]}}) == 4
    assert result_count({"data": {"summary": "one"}}) == 1
    assert result_count({"ok": True}) == 1


def test_a_compiled_schema_is_reused_across_calls() -> None:
    """check_schema dominates the cost, so it must not run per call.

    A separate dict with identical content keys to the same compiled validator.
    """
    from harborrag_engine.tools.budgets import _compiled, _validator

    _compiled.cache_clear()
    spec = _spec()
    budget = ToolBudget(label="MCP")

    budget.check_call(spec, {"query": "x"})
    budget.check_call(spec, {"query": "y"})
    assert _validator(dict(_SCHEMA)) is _validator(spec.input_schema)
    assert _compiled.cache_info().hits >= 2
