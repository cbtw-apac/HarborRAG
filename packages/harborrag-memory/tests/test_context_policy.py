from __future__ import annotations

import pytest

from harborrag_memory import MemoryPolicy, approximate_tokens
from harborrag_memory.context import DEFAULT_RECALL_SCOPES, summary_memory_id
from harborrag_memory.context.condenser import sanitize_query
from harborrag_memory.errors import MemoryConfigurationError
from harborrag_memory.schemas import MemoryScope

pytestmark = [pytest.mark.unit]


def test_default_policy_is_enabled_and_self_consistent() -> None:
    policy = MemoryPolicy()

    assert policy.enabled is True
    assert policy.recent_max_messages == 12
    assert policy.recent_max_tokens == 2000
    assert policy.summary_keep_messages == 8
    assert policy.recall_top_k == 6
    assert (
        policy.recall_scopes
        == DEFAULT_RECALL_SCOPES
        == (
            MemoryScope.USER,
            MemoryScope.PROJECT,
            MemoryScope.SESSION,
            MemoryScope.TENANT,
        )
    )
    assert policy.query_rewrite is True
    assert policy.extraction_min_importance == 0.3
    assert policy.dedup_threshold == 0.92
    assert policy.summary_keep_messages <= policy.recent_max_messages


def test_disabled_policy_turns_off_every_model_call() -> None:
    policy = MemoryPolicy.disabled()

    assert policy.enabled is False
    assert policy.recent_max_messages == 4
    assert policy.recall_top_k == 0
    assert policy.query_rewrite is False
    assert policy.dedup_threshold == 0.92
    assert policy.summary_keep_messages <= policy.recent_max_messages


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary_keep_messages": 13},
        {"recent_max_messages": 0},
        {"recent_max_tokens": -1},
        {"summary_trigger_fraction": 0.0},
        {"summary_trigger_fraction": 1.5},
        {"block_budget_fraction": 0.0},
        {"recency_half_life_hours": 0.0},
        {"recall_top_k": -1},
        {"recall_scopes": (MemoryScope.USER, MemoryScope.USER)},
        {"extraction_min_importance": -0.1},
        {"extraction_min_importance": 1.1},
        {"dedup_threshold": 0.4},
        {"dedup_threshold": 1.1},
    ],
)
def test_policy_validation_rejects_incoherent_values(overrides: dict[str, object]) -> None:
    with pytest.raises(MemoryConfigurationError):
        MemoryPolicy(**overrides)  # type: ignore[arg-type]


def test_approximate_tokens_matches_the_runtime_estimator() -> None:
    assert approximate_tokens("") == 0
    assert approximate_tokens("a") == 1
    assert approximate_tokens("abcd" * 10) == 10
    assert approximate_tokens("漢字") == 2
    assert approximate_tokens("abcd漢") == 2


def test_summary_memory_id_is_deterministic_per_session() -> None:
    assert summary_memory_id("s-1") == summary_memory_id("s-1") == "summary:s-1"
    assert summary_memory_id("s-2") != summary_memory_id("s-1")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"Who owns the ingest pipeline?"', "Who owns the ingest pipeline?"),
        ("  Who owns it?  ", "Who owns it?"),
        ("", None),
        ("   ", None),
        ("x" * 300, None),
    ],
)
def test_sanitize_query_unwraps_quotes_and_rejects_junk(text: str, expected: str | None) -> None:
    assert sanitize_query(text, question="and who owns it?") == expected
