from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_core.config_store.levels import SettingsLevel
from harborrag_core.config_store.registry import SettingsRegistry
from harborrag_core.config_store.resolution import (
    EffectiveValue,
    SettingChange,
    SettingsReindexRequiredError,
    SettingsSnapshot,
    plan_changes,
    resolve_effective,
    revision_label,
)
from harborrag_core.contracts.errors import HarborValidationError

pytestmark = pytest.mark.unit

KEY = "chunking.max_tokens"
REGISTRY = SettingsRegistry.default()
SNAPSHOT = SettingsSnapshot(
    tenant_id="acme",
    revision=3,
    overrides={
        "tenant": {KEY: 512},
        "project:demo": {KEY: 384},
        "connection:demo/demo-tickets": {KEY: 256},
    },
)


def test_effective_precedence() -> None:
    def effective(text: str) -> EffectiveValue:
        return resolve_effective(REGISTRY, SNAPSHOT, SettingsLevel.parse(text)).values[KEY]

    assert effective("connection:demo/demo-tickets") == EffectiveValue(256, "connection")
    assert effective("connection:demo/demo-wiki") == EffectiveValue(384, "project")
    assert effective("connection:hr/hr-docs") == EffectiveValue(512, "tenant")
    resolved = resolve_effective(REGISTRY, SNAPSHOT, SettingsLevel("tenant"))
    assert resolved.values["retrieval.top_k"] == EffectiveValue(5, "default")
    assert resolved.revision == 3
    assert set(resolved.values) == set(REGISTRY.keys())
    assert resolved.value(KEY) == 512
    assert resolved.override(KEY) == 512
    assert resolved.override("retrieval.top_k") is None


def test_override_at_a_level_the_key_does_not_list_is_ignored() -> None:
    snapshot = SettingsSnapshot("acme", 1, {"project:demo": {"ingestion.concurrency": 2}})

    resolved = resolve_effective(REGISTRY, snapshot, SettingsLevel.parse("project:demo"))

    assert resolved.values["ingestion.concurrency"] == EffectiveValue(8, "default")


def plan(
    level: str, body: dict[str, object], snapshot: SettingsSnapshot = SNAPSHOT
) -> tuple[SettingChange, ...]:
    return plan_changes(REGISTRY, snapshot, SettingsLevel.parse(level), body)


def test_plan_changes_values() -> None:
    (change,) = plan("project:demo", {KEY: 300})
    assert (change.from_value, change.to_value, change.applies) == (384, 300, "next_ingestion")
    assert change.level == "project:demo"
    assert change.as_dict() == {"key": KEY, "from": 384, "to": 300, "applies": "next_ingestion"}

    (removal,) = plan("project:demo", {KEY: None})
    assert removal.to_value is None

    without_project = SettingsSnapshot("acme", 3, {"tenant": {KEY: 512}})
    (inherited,) = plan("project:demo", {KEY: 384}, without_project)
    assert inherited.from_value == 512

    assert plan("project:demo", {KEY: 384}) == ()
    assert plan("project:hr", {KEY: None}) == ()


@pytest.mark.parametrize(
    ("level", "body", "field"),
    [
        ("tenant", {"chunking.max_tokenz": 1}, "chunking.max_tokenz"),
        ("tenant", {KEY: 99999}, KEY),
        ("connection:demo/demo-tickets", {"retrieval.top_k": 5}, "retrieval.top_k"),
        ("tenant", {"database.url": "x"}, "database.url"),
        ("tenant", {"retrieval.rerank": True}, "retrieval.rerank"),
        ("tenant", {KEY: True}, KEY),
    ],
)
def test_plan_changes_refusals(level: str, body: dict[str, object], field: str) -> None:
    with pytest.raises(HarborValidationError) as raised:
        plan(level, body)

    assert raised.value.details == {"field": field}


def test_boot_only_refusal_names_the_cause() -> None:
    with pytest.raises(HarborValidationError, match="boot-only"):
        plan("tenant", {"database.url": "x"})


def test_reindex_key_is_a_conflict() -> None:
    with pytest.raises(SettingsReindexRequiredError) as raised:
        plan("tenant", {"models.embedding": "openai:te3"})

    assert raised.value.error_code == "reindex_required"


def test_rerank_false_is_accepted() -> None:
    (change,) = plan("tenant", {"retrieval.rerank": False})

    assert change.to_value is False


def test_rerank_true_is_accepted_with_a_rerank_model() -> None:
    registry = replace(REGISTRY, rerank_available=True)

    (change,) = plan_changes(
        registry, SNAPSHOT, SettingsLevel("tenant"), {"retrieval.rerank": True}
    )

    assert change.to_value is True


OVERLAP = "chunking.overlap_tokens"
TICKETS = "connection:demo/demo-tickets"


TENANT_OVERLAP = replace(
    SNAPSHOT, overrides={**SNAPSHOT.overrides, "tenant": {KEY: 512, OVERLAP: 100}}
)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({OVERLAP: 256}, OVERLAP),
        ({OVERLAP: 300, KEY: 300}, OVERLAP),
        ({KEY: 100}, KEY),
    ],
)
def test_overlap_at_or_above_max_tokens_is_refused(body: dict[str, object], field: str) -> None:
    with pytest.raises(HarborValidationError, match="must be below") as raised:
        plan(TICKETS, body, TENANT_OVERLAP)

    assert raised.value.details == {"field": field}


def test_max_tokens_is_not_compared_with_the_default_overlap() -> None:
    (change,) = plan(TICKETS, {KEY: 64})

    assert change.to_value == 64


def test_overlap_check_uses_the_value_a_removed_override_falls_back_to() -> None:
    snapshot = replace(
        SNAPSHOT, overrides={**SNAPSHOT.overrides, TICKETS: {KEY: 256, OVERLAP: 100}}
    )

    removal, overlap = plan(TICKETS, {KEY: None, OVERLAP: 300}, snapshot)

    assert (removal.key, removal.to_value) == (KEY, None)
    assert (overlap.key, overlap.to_value) == (OVERLAP, 300)
    with pytest.raises(HarborValidationError) as raised:
        plan(TICKETS, {KEY: None, OVERLAP: 384}, snapshot)
    assert raised.value.details == {"field": OVERLAP}


def test_first_failure_in_sorted_key_order_wins() -> None:
    with pytest.raises(HarborValidationError) as raised:
        plan("tenant", {"retrieval.top_k": 0, KEY: 1})

    assert raised.value.details == {"field": KEY}


def test_revision_label() -> None:
    assert revision_label(41) == "rev_41"
