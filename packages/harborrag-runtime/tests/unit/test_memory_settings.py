"""Conversation-memory policy settings on RuntimeSettings."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from harborrag_core.ports.memory import MemoryScope
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_ambient_harborrag_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decide these cases from their arguments, never from the ambient environment."""

    for name in [key for key in os.environ if key.startswith("HARBORRAG_")]:
        monkeypatch.delenv(name, raising=False)


def test_defaults_match_the_approved_policy() -> None:
    settings = RuntimeSettings()

    assert settings.memory_enabled is True
    assert settings.memory_recent_max_messages == 12
    assert settings.memory_recent_max_tokens == 2_000
    assert settings.memory_summary_trigger_fraction == pytest.approx(0.7)
    assert settings.memory_summary_keep_messages == 8
    assert settings.memory_recall_top_k == 6
    assert settings.memory_recall_recency_half_life_hours == pytest.approx(168.0)
    assert settings.memory_block_budget_fraction == pytest.approx(0.15)
    assert settings.memory_type_affinity_weight == pytest.approx(0.5)
    assert settings.memory_query_rewrite is True
    # Entity linking is on: memory is joined to the graph by id, never written into it.
    assert settings.memory_entity_linking is True
    # The agent memory tools are opt-in.
    assert settings.memory_agent_tools is False
    assert settings.memory_extraction_enabled is True
    assert settings.memory_extraction_min_importance == pytest.approx(0.3)
    assert settings.memory_dedup_threshold == pytest.approx(0.92)
    assert settings.memory_model_profile == "memory"
    assert settings.memory_embed_profile is None
    assert settings.memory_pii_redaction is False


def test_environment_overrides_use_the_harborrag_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBORRAG_MEMORY_ENABLED", "false")
    monkeypatch.setenv("HARBORRAG_MEMORY_RECALL_TOP_K", "12")
    monkeypatch.setenv("HARBORRAG_MEMORY_MODEL_PROFILE", "cheap")
    monkeypatch.setenv("HARBORRAG_MEMORY_ENTITY_LINKING", "false")
    monkeypatch.setenv("HARBORRAG_MEMORY_AGENT_TOOLS", "true")
    monkeypatch.setenv("HARBORRAG_MEMORY_TYPE_AFFINITY_WEIGHT", "1.5")

    settings = RuntimeSettings()

    assert settings.memory_enabled is False
    assert settings.memory_recall_top_k == 12
    assert settings.memory_model_profile == "cheap"
    assert settings.memory_entity_linking is False
    assert settings.memory_agent_tools is True
    assert settings.memory_type_affinity_weight == pytest.approx(1.5)


def test_recall_scopes_parse_in_priority_order() -> None:
    settings = RuntimeSettings(memory_recall_scopes="  USER , Session ")

    assert settings.memory_recall_scopes == "user,session"
    assert settings.memory_recall_scope_order == (MemoryScope.USER, MemoryScope.SESSION)


def test_default_recall_scopes_cover_personal_shared_and_tenant_memory() -> None:
    assert RuntimeSettings().memory_recall_scope_order == (
        MemoryScope.USER,
        MemoryScope.PROJECT,
        MemoryScope.SESSION,
        MemoryScope.TENANT,
    )


@pytest.mark.parametrize("value", ["", "   ", ","])
def test_recall_scopes_reject_an_empty_list(value: str) -> None:
    with pytest.raises(ValidationError, match="at least one scope"):
        RuntimeSettings(memory_recall_scopes=value)


@pytest.mark.parametrize("value", ["run", "global", "user,nonsense"])
def test_recall_scopes_reject_unrecallable_scopes(value: str) -> None:
    """RUN is per-run scratch and GLOBAL is not owned by any tenant."""

    with pytest.raises(ValidationError, match="unrecallable scopes"):
        RuntimeSettings(memory_recall_scopes=value)


def test_recall_scopes_reject_duplicates() -> None:
    with pytest.raises(ValidationError, match="must not repeat"):
        RuntimeSettings(memory_recall_scopes="user,user")


def test_verbatim_tail_must_fit_inside_the_recent_window() -> None:
    with pytest.raises(ValidationError, match="must not exceed"):
        RuntimeSettings(memory_recent_max_messages=4, memory_summary_keep_messages=8)


def test_verbatim_tail_may_equal_the_window() -> None:
    settings = RuntimeSettings(memory_recent_max_messages=8, memory_summary_keep_messages=8)

    assert settings.memory_summary_keep_messages == 8


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_recent_max_messages", 0),
        ("memory_recent_max_tokens", 10),
        ("memory_summary_trigger_fraction", 1.5),
        ("memory_recall_top_k", 51),
        ("memory_recall_recency_half_life_hours", -1.0),
        ("memory_recall_recency_half_life_hours", 0.0),
        ("memory_summary_keep_messages", 0),
        ("memory_block_budget_fraction", 0.9),
        ("memory_block_budget_fraction", 0.0),
        ("memory_type_affinity_weight", -0.1),
        ("memory_type_affinity_weight", 2.1),
        ("memory_extraction_min_importance", 1.2),
        ("memory_dedup_threshold", 0.2),
        ("memory_retention_days_user", -1),
        ("memory_model_profile", ""),
    ],
)
def test_out_of_range_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(**{field: value})


def test_recall_top_k_zero_disables_long_term_recall() -> None:
    """Zero is legal: the window and summary still apply, recall does not run."""

    assert RuntimeSettings(memory_recall_top_k=0).memory_recall_top_k == 0


def test_retention_days_resolve_per_scope() -> None:
    settings = RuntimeSettings(
        memory_retention_days_session=30,
        memory_retention_days_user=180,
        memory_retention_days_project=0,
    )

    assert settings.memory_retention_days(MemoryScope.SESSION) == 30
    assert settings.memory_retention_days(MemoryScope.RUN) == 30
    assert settings.memory_retention_days(MemoryScope.USER) == 180
    assert settings.memory_retention_days(MemoryScope.PROJECT) == 0
    assert settings.memory_retention_days(MemoryScope.TENANT) == 0
    assert settings.memory_retention_days(MemoryScope.GLOBAL) == 0


@pytest.mark.parametrize("field", ["memory_entity_linking", "memory_agent_tools"])
def test_the_new_memory_flags_are_booleans(field: str) -> None:
    assert RuntimeSettings(**{field: True}).__getattribute__(field) is True
    assert RuntimeSettings(**{field: False}).__getattribute__(field) is False
    with pytest.raises(ValidationError):
        RuntimeSettings(**{field: "sometimes"})


@pytest.mark.parametrize("value", [0.0, 2.0])
def test_type_affinity_weight_accepts_the_whole_range(value: float) -> None:
    """0 disables the type term without disabling anything else; 2 is the ceiling."""

    assert RuntimeSettings(memory_type_affinity_weight=value).memory_type_affinity_weight == (
        pytest.approx(value)
    )
