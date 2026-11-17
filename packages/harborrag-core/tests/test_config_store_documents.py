from __future__ import annotations

import pytest

from harborrag_core.config_store import (
    SettingsLevel,
    SettingsRegistry,
    SettingsSnapshot,
)
from harborrag_core.config_store.documents import (
    SettingsDocument,
    SkippedSetting,
    drift_keys,
    export_document,
    plan_import,
)
from harborrag_core.contracts.errors import HarborValidationError

pytestmark = pytest.mark.unit

KEY = "chunking.max_tokens"
REGISTRY = SettingsRegistry.default()
SNAPSHOT = SettingsSnapshot(
    tenant_id="acme",
    revision=3,
    overrides={"tenant": {KEY: 512}, "project:cpm": {KEY: 384, "retrieval.top_k": 9}},
)


def _document(**levels: dict[str, object]) -> SettingsDocument:
    return SettingsDocument({label.replace("__", ":"): body for label, body in levels.items()})


def test_plan_import_diff() -> None:
    plan = plan_import(
        REGISTRY,
        SNAPSHOT,
        _document(tenant={KEY: 1024, "ingestion.concurrency": 4}),
        prune=False,
    )

    assert plan.tenant_id == "acme"
    assert plan.base_revision == 3
    assert [
        (change.level, change.key, change.from_value, change.to_value, change.applies)
        for change in plan.changes
    ] == [
        ("tenant", KEY, 512, 1024, "next_ingestion"),
        ("tenant", "ingestion.concurrency", 8, 4, "next_ingestion"),
    ]
    assert plan.skipped == ()
    assert not plan.empty


def test_plan_import_keeps_missing_key_unless_prune() -> None:
    document = _document(tenant={KEY: 512})

    kept = plan_import(REGISTRY, SNAPSHOT, document, prune=False)
    pruned = plan_import(REGISTRY, SNAPSHOT, document, prune=True)

    assert kept.empty
    assert [(change.level, change.key, change.to_value) for change in pruned.changes] == [
        ("project:cpm", KEY, None),
        ("project:cpm", "retrieval.top_k", None),
    ]


def test_plan_import_skips_boot_only_and_reindex() -> None:
    plan = plan_import(
        REGISTRY,
        SNAPSHOT,
        _document(tenant={"database.url": "x", "models.embedding": "openai:text-embed", KEY: 512}),
        prune=False,
    )

    assert plan.empty
    assert plan.skipped == (
        SkippedSetting("tenant", "database.url", "boot_only"),
        SkippedSetting("tenant", "models.embedding", "reindex_required"),
    )


def test_plan_import_ignores_reindex_key_equal_to_effective() -> None:
    plan = plan_import(
        REGISTRY, SNAPSHOT, _document(tenant={"models.embedding": None}), prune=False
    )

    assert plan.empty
    assert plan.skipped == ()


def test_plan_import_rejects_bad_value() -> None:
    with pytest.raises(HarborValidationError) as error:
        plan_import(REGISTRY, SNAPSHOT, _document(tenant={KEY: "big"}), prune=False)

    assert error.value.details == {"field": KEY, "level": "tenant"}


def test_export_then_plan_is_empty() -> None:
    document = export_document(SNAPSHOT, None)

    assert plan_import(REGISTRY, SNAPSHOT, document, prune=True).empty
    assert export_document(SNAPSHOT, SettingsLevel.parse("project:cpm")).levels == {
        "project:cpm": {KEY: 384, "retrieval.top_k": 9}
    }


def test_drift_keys() -> None:
    document = _document(
        tenant={KEY: 1024, "database.url": "x", "models.embedding": "a:b"},
        project__cpm={KEY: 384},
    )

    assert drift_keys(REGISTRY, SNAPSHOT, document) == (KEY, "retrieval.top_k")
    assert drift_keys(REGISTRY, SNAPSHOT, export_document(SNAPSHOT, None)) == ()


def test_plan_import_prune_skips_stored_reindex_key() -> None:
    snapshot = SettingsSnapshot("acme", 1, {"tenant": {KEY: 512, "models.embedding": "a:b"}})

    plan = plan_import(REGISTRY, snapshot, _document(), prune=True)

    assert [(change.key, change.to_value) for change in plan.changes] == [(KEY, None)]
    assert plan.skipped == (SkippedSetting("tenant", "models.embedding", "reindex_required"),)
