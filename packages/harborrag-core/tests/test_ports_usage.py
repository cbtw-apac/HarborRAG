"""Contract coverage for the model-usage accounting port."""

from __future__ import annotations

from datetime import datetime

import pytest

from harborrag_core.ports.usage import (
    ModelUsageRecord,
    ModelUsageTotals,
    new_usage_id,
)

pytestmark = pytest.mark.unit


def _usage(**overrides: object) -> ModelUsageRecord:
    fields: dict[str, object] = {
        "tenant_id": "ACME",
        "user_id": "user-1",
        "principal_id": "principal-1",
        "surface": "chat",
        "logical_model": "chat-default",
        "provider": "openai",
        "provider_model": "gpt-4.1-mini",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    fields.update(overrides)
    return ModelUsageRecord(**fields)  # type: ignore[arg-type]


@pytest.mark.whitebox
def test_a_usage_record_defaults_its_id_and_timestamp() -> None:
    usage = _usage()

    assert usage.id.startswith("usage-")
    assert usage.created_at.tzinfo is not None
    assert usage.session_id is None
    assert usage.estimated_cost_usd is None
    assert new_usage_id() != new_usage_id()


@pytest.mark.whitebox
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"tenant_id": " "}, "requires a tenant and a user"),
        ({"user_id": ""}, "requires a tenant and a user"),
        ({"prompt_tokens": -1}, "must not be negative"),
        ({"estimated_cost_usd": -0.01}, "must not be negative"),
        ({"created_at": datetime(2026, 1, 1)}, "timezone-aware"),  # noqa: DTZ001
    ],
)
def test_a_usage_record_rejects_unusable_values(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _usage(**overrides)


@pytest.mark.whitebox
def test_totals_default_to_zero() -> None:
    assert ModelUsageTotals() == ModelUsageTotals(0, 0, 0, 0, 0.0)
