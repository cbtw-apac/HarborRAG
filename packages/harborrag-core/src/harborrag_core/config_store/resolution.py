"""Effective values and change planning over a settings snapshot.

Pure; no I/O. Resolution runs default, tenant, project, connection and the most
specific override wins. Planning checks keys in sorted order and the first
failure raises, so nothing is ever partially planned.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from harborrag_core.config_store.levels import SettingsLevel
from harborrag_core.config_store.registry import Applies, SettingsRegistry
from harborrag_core.contracts.errors import HarborConflictError, HarborValidationError

Origin = Literal["default", "tenant", "project", "connection"]
RevisionSource = Literal["seed", "import", "api"]


class SettingsReindexRequiredError(HarborConflictError):
    """The key needs a reindex, which this release cannot start from a settings change."""

    error_code = "reindex_required"


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """All overrides of one tenant at one revision.

    Args:
        tenant_id: Tenant that owns the overrides.
        revision: Tenant revision counter; 0 for an empty log.
        overrides: Key to value maps keyed by level label.
    """

    tenant_id: str
    revision: int
    overrides: Mapping[str, Mapping[str, object]]

    def at(self, level: SettingsLevel) -> Mapping[str, object]:
        """Return the overrides stored at exactly ``level``."""
        return self.overrides.get(str(level), {})


@dataclass(frozen=True, slots=True)
class EffectiveValue:
    """A resolved value and the level it came from."""

    value: object
    origin: Origin


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    """Every registry key resolved at one level.

    Args:
        revision: Revision of the snapshot resolved.
        values: Effective value by key.
    """

    revision: int
    values: Mapping[str, EffectiveValue]

    def value(self, key: str) -> object:
        """Return the effective value of ``key``."""
        return self.values[key].value

    def override(self, key: str) -> object | None:
        """Return the value of ``key`` when some level overrides it, else ``None``."""
        effective = self.values[key]
        return None if effective.origin == "default" else effective.value


@dataclass(frozen=True, slots=True)
class SettingChange:
    """One planned or recorded override change.

    Args:
        level: Level label the override lives at.
        key: Setting key.
        from_value: Effective value at the level before the change.
        to_value: New override; ``None`` removes it.
        applies: When the change takes effect.
    """

    level: str
    key: str
    from_value: object
    to_value: object | None
    applies: Applies

    def as_dict(self) -> dict[str, object]:
        """Return the ``changed[]`` item shape of the API."""
        return {
            "key": self.key,
            "from": self.from_value,
            "to": self.to_value,
            "applies": self.applies,
        }


@dataclass(frozen=True, slots=True)
class SettingsRevision:
    """One entry of a tenant's revision log."""

    tenant_id: str
    revision: int
    source: RevisionSource
    created_by: str
    created_at: datetime
    changes: tuple[SettingChange, ...]


def revision_label(revision: int) -> str:
    """Return the public label of ``revision``, ``rev_<N>``."""
    return f"rev_{revision}"


def resolve_effective(
    registry: SettingsRegistry, snapshot: SettingsSnapshot, level: SettingsLevel
) -> EffectiveSettings:
    """Resolve every registry key at ``level`` through its chain.

    Args:
        registry: Registry whose keys and defaults apply.
        snapshot: Overrides to resolve over.
        level: Level to resolve at.

    Returns:
        Each key's value with the level kind it came from, or ``default``.
    """
    values: dict[str, EffectiveValue] = {}
    for key in registry.keys():
        definition = registry.definitions[key]
        resolved = EffectiveValue(definition.default, "default")
        for step in level.chain():
            overrides = snapshot.at(step)
            if step.kind in definition.levels and key in overrides:
                resolved = EffectiveValue(overrides[key], step.kind)
        values[key] = resolved
    return EffectiveSettings(snapshot.revision, values)


def plan_changes(
    registry: SettingsRegistry,
    snapshot: SettingsSnapshot,
    level: SettingsLevel,
    body: Mapping[str, object],
) -> tuple[SettingChange, ...]:
    """Plan the override changes a PATCH body makes at ``level``.

    Keys are checked in sorted order: known and not boot-only, allowed at the
    level, not a reindex key, ``retrieval.rerank: true`` only with a rerank model,
    and a valid value. A ``None`` value removes the override. Unchanged overrides
    are left out.

    Args:
        registry: Registry the keys must belong to.
        snapshot: Current overrides.
        level: Level the body applies to.
        body: Key to new value; ``None`` removes the override.

    Returns:
        The changes, in key order.

    Raises:
        HarborValidationError: First key that is unknown, boot-only, not allowed at
            the level, ``retrieval.rerank: true`` without a rerank model, or invalid
            (``field = key``).
        SettingsReindexRequiredError: A key whose change needs a reindex.
    """
    effective = resolve_effective(registry, snapshot, level)
    stored = snapshot.at(level)
    changes: list[SettingChange] = []
    for key in sorted(body):
        definition = registry.definition(key)
        if level.kind not in definition.levels:
            raise HarborValidationError(f"{key} cannot be set at {level}", details={"field": key})
        if definition.applies == "reindex":
            raise SettingsReindexRequiredError(
                f"{key} needs a reindex, which cannot be started here"
            )
        value = body[key]
        if key == "retrieval.rerank" and value is True and not registry.rerank_available:
            raise HarborValidationError(
                "retrieval.rerank cannot be enabled: the model catalog has no rerank model",
                details={"field": key},
            )
        new = None if value is None else definition.validate(value)
        if new is None and key not in stored:
            continue
        if new is not None and key in stored and stored[key] == new:
            continue
        changes.append(
            SettingChange(str(level), key, effective.value(key), new, definition.applies)
        )
    return tuple(changes)
