"""Summarization config is a top-level section with unit-bearing, grouped names."""

from __future__ import annotations

from decimal import Decimal

import pytest

from harborrag_runtime.config.errors import GraphBuildConfigurationError
from harborrag_runtime.config.graph_build import GraphBuildConfig
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.unit

_YAML = """summarization:
  enabled: true
  model: summariser
  max_description_tokens: 1024
  budget:
    max_usd_per_run: "5.00"
    max_calls_per_document: 128
  input:
    max_children_per_call: 4
    max_bytes_per_call: 20000
    max_tokens_per_call: 5000
"""


def _config(tmp_path, payload: str = _YAML) -> GraphBuildConfig:
    path = tmp_path / "graph_build.yaml"
    path.write_text(payload, encoding="utf-8")
    return GraphBuildConfig.from_file(path)


def test_the_three_headline_knobs_read_plainly(tmp_path) -> None:
    summarization = _config(tmp_path).summarization

    assert summarization.model == "summariser"
    assert summarization.max_description_tokens == 1024
    assert summarization.budget.max_usd_per_run == Decimal("5.00")


def test_budget_and_input_limits_are_grouped_by_what_they_bound(tmp_path) -> None:
    summarization = _config(tmp_path).summarization

    assert summarization.budget.max_calls_per_document == 128
    assert summarization.input.max_children_per_call == 4
    assert summarization.input.max_bytes_per_call == 20000
    assert summarization.input.max_tokens_per_call == 5000


def test_a_minimal_section_needs_only_enabled(tmp_path) -> None:
    summarization = _config(tmp_path, "summarization:\n  enabled: true\n").summarization

    assert summarization.enabled is True
    assert summarization.model is None
    assert summarization.budget.max_usd_per_run is None


def test_it_is_off_unless_asked_for(tmp_path) -> None:
    assert _config(tmp_path, "tenants: []\n").summarization.enabled is False


def test_a_misplaced_legacy_key_is_rejected_loudly(tmp_path) -> None:
    payload = "summarization:\n  enabled: true\n  parent_max_output_tokens: 512\n"

    with pytest.raises(GraphBuildConfigurationError):
        _config(tmp_path, payload)


def test_the_section_reaches_the_runtime(tmp_path) -> None:
    settings = _config(tmp_path).effective_settings(RuntimeSettings())

    assert settings.topology_parent_enabled is True
    assert settings.topology_parent_model == "summariser"
    assert settings.topology_parent_run_budget_usd == Decimal("5.00")
    assert settings.topology_parent_max_output_tokens == 1024
    assert settings.topology_parent_max_fan_in == 4
