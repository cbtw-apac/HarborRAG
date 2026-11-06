"""Settings documents: planning an import, exporting overrides and finding drift.

Pure; no I/O. A document holds the overrides a settings file declares, keyed by
level label and then by dotted key. YAML reading and writing live in runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from harborrag_core.config_store.levels import SettingsLevel
from harborrag_core.config_store.registry import SettingsRegistry, is_boot_only
from harborrag_core.config_store.resolution import (
    EffectiveSettings,
    SettingChange,
    SettingsSnapshot,
    plan_changes,
    resolve_effective,
)
from harborrag_core.contracts.errors import HarborValidationError

SkipReason = Literal["boot_only", "reindex_required"]
_ABSENT = object()


@dataclass(frozen=True, slots=True)
class SettingsDocument:
    """Overrides declared by a settings file.

    Args:
        levels: Key to value maps keyed by level label.
    """

    levels: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class SkippedSetting:
    """A key an import reports and leaves alone."""

    level: str
    key: str
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """What applying a document would change.

    Args:
        tenant_id: Tenant the plan is for.
        base_revision: Revision the plan was made against.
        changes: Override changes, level by level in key order.
        skipped: Keys reported and not applied.
    """

    tenant_id: str
    base_revision: int
    changes: tuple[SettingChange, ...]
    skipped: tuple[SkippedSetting, ...]

    @property
    def empty(self) -> bool:
        """Whether applying the plan writes nothing."""
        return not self.changes


def _level_order(labels: set[str]) -> list[str]:
    return sorted(labels, key=lambda label: (label != "tenant", label))


def _split_skipped(
    registry: SettingsRegistry, effective: EffectiveSettings, label: str, body: Mapping[str, object]
) -> tuple[dict[str, object], list[SkippedSetting]]:
    kept: dict[str, object] = {}
    skipped: list[SkippedSetting] = []
    for key in sorted(body):
        if is_boot_only(key):
            skipped.append(SkippedSetting(label, key, "boot_only"))
            continue
        definition = registry.definitions.get(key)
        if definition is not None and definition.applies == "reindex":
            if body[key] != effective.value(key):
                skipped.append(SkippedSetting(label, key, "reindex_required"))
            continue
        kept[key] = body[key]
    return kept, skipped


def plan_import(
    registry: SettingsRegistry,
    snapshot: SettingsSnapshot,
    document: SettingsDocument,
    *,
    prune: bool,
) -> ImportPlan:
    """Plan the changes that make the stored overrides match ``document``.

    Each level goes through ``plan_changes``. Boot-only keys, and reindex keys whose
    value differs from the effective one, are skipped with a reason.

    Args:
        registry: Registry the keys must belong to.
        snapshot: Current overrides.
        document: Overrides the file declares.
        prune: Also remove every stored override the document lacks, at any level.

    Returns:
        The plan, made against ``snapshot.revision``.

    Raises:
        HarborValidationError: A key is unknown, not allowed at its level or invalid
            (``field = key``, ``level = <level label>``).
    """
    labels = set(document.levels) | (set(snapshot.overrides) if prune else set())
    changes: list[SettingChange] = []
    skipped: list[SkippedSetting] = []
    for label in _level_order(labels):
        level = SettingsLevel.parse(label)
        effective = resolve_effective(registry, snapshot, level)
        body, level_skipped = _split_skipped(
            registry, effective, label, document.levels.get(label, {})
        )
        skipped.extend(level_skipped)
        if prune:
            body |= {
                key: None
                for key in snapshot.at(level)
                if key not in document.levels.get(label, {}) and key in registry.definitions
            }
        try:
            changes.extend(plan_changes(registry, snapshot, level, body))
        except HarborValidationError as error:
            raise HarborValidationError(
                str(error), details={**error.details, "level": label}
            ) from error
    return ImportPlan(snapshot.tenant_id, snapshot.revision, tuple(changes), tuple(skipped))


def export_document(snapshot: SettingsSnapshot, level: SettingsLevel | None) -> SettingsDocument:
    """Return the stored overrides of ``level``, or of every level when ``None``."""
    labels = [str(level)] if level is not None else _level_order(set(snapshot.overrides))
    return SettingsDocument(
        {
            label: dict(sorted(snapshot.overrides[label].items()))
            for label in labels
            if snapshot.overrides.get(label)
        }
    )


def drift_keys(
    registry: SettingsRegistry, snapshot: SettingsSnapshot, document: SettingsDocument
) -> tuple[str, ...]:
    """Return the sorted keys whose file and stored values differ, in either direction.

    Boot-only and reindex keys are ignored.
    """
    drifted: set[str] = set()
    for label in set(document.levels) | set(snapshot.overrides):
        declared = _present(document.levels.get(label, {}))
        stored = snapshot.overrides.get(label, {})
        for key in declared.keys() | stored.keys():
            definition = registry.definitions.get(key)
            if is_boot_only(key) or (definition is not None and definition.applies == "reindex"):
                continue
            if declared.get(key, _ABSENT) != stored.get(key, _ABSENT):
                drifted.add(key)
    return tuple(sorted(drifted))


def _present(body: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in body.items() if value is not None}
