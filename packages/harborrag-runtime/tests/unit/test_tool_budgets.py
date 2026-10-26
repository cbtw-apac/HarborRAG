"""The ceilings both transports enforce around one tool call."""

from __future__ import annotations

import pytest

from harborrag_engine.tools.base import ToolSpec
from harborrag_engine.tools.budgets import ToolBudget, result_count

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
    return ToolSpec(**fields)  # type: ignore[arg-type]


def test_an_ingestion_tool_is_refused_unless_it_is_switched_on() -> None:
    budget = ToolBudget(label="Agent")

    with pytest.raises(PermissionError, match="ingestion tools are disabled"):
        budget.check_call(_spec(capability="ingestion"), {"query": "x"})

    ToolBudget(label="Agent", allow_ingestion=True).check_call(
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
    budget = ToolBudget(label="Agent")
    broken = _spec(input_schema={"type": "not-a-type"})

    with pytest.raises(RuntimeError, match="invalid input schema"):
        budget.check_call(broken, {"query": "x"})
    with pytest.raises(RuntimeError, match="invalid output schema"):
        budget.check_output_schema({"ok": True}, {"type": "not-a-type"})
    with pytest.raises(RuntimeError, match="has no output schema"):
        budget.check_output_schema({"ok": True}, None)
    with pytest.raises(RuntimeError, match="returned an invalid output"):
        budget.check_output_schema({"ok": "yes"}, {"type": "object", "required": ["missing"]})


def test_detail_is_offered_only_where_a_model_has_to_self_correct() -> None:
    """MCP reports the bare contract; the agent loop needs the failing field."""

    quiet = ToolBudget(label="MCP")
    loud = ToolBudget(label="Agent", detail_in_errors=True)

    with pytest.raises(ValueError) as silent:
        quiet.check_call(_spec(), {"query": "far too long"})
    with pytest.raises(ValueError) as spoken:
        loud.check_call(_spec(), {"query": "far too long"})

    assert str(silent.value) == "MCP arguments do not match the tool schema."
    assert "$.query" in str(spoken.value)


def test_a_long_detail_is_truncated_rather_than_forwarded_whole() -> None:
    budget = ToolBudget(label="Agent", detail_in_errors=True)
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
    """check_schema dominates the cost, so it must not run per call."""

    from harborrag_engine.tools.budgets import _compiled, _validator

    _compiled.cache_clear()
    spec = _spec()
    budget = ToolBudget(label="Agent")

    budget.check_call(spec, {"query": "x"})
    budget.check_call(spec, {"query": "y"})
    # A separate dict with identical content keys to the same compiled validator.
    assert _validator(dict(_SCHEMA)) is _validator(spec.input_schema)
    assert _compiled.cache_info().hits >= 2
