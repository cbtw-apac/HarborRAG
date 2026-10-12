"""Typed connector configuration whose fields say what a change means.

Every field is tagged with a :class:`FieldClass` through ``Annotated``:

* ``SCOPE`` decides *which* items are read. Changing it re-scopes the source,
  with a backfill for anything newly in scope and a reconcile for anything
  that fell out.
* ``RENDER`` decides *how* items are rendered. Changing it re-fetches
  everything, because every body changes.
* ``COSMETIC`` changes nothing that is stored, such as page sizes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from .errors import ConfigError


class FieldClass(StrEnum):
    """What changing a configuration field means for already-indexed data."""

    SCOPE = "scope"
    RENDER = "render"
    COSMETIC = "cosmetic"


class ConnectorConfig(BaseModel):
    """Base for connector configuration: immutable, closed to unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def field_classes(model: type[BaseModel]) -> dict[str, FieldClass]:
    """Return each field's class; raise when a field carries no tag."""

    classes: dict[str, FieldClass] = {}
    for name, info in model.model_fields.items():
        tags = [item for item in info.metadata if isinstance(item, FieldClass)]
        if len(tags) != 1:
            raise ConfigError(
                f"{model.__name__}.{name} must be annotated with exactly one FieldClass"
            )
        classes[name] = tags[0]
    return classes


@dataclass(frozen=True, slots=True)
class ConfigDiff:
    """Changed field names, grouped by what the change means."""

    scope: frozenset[str] = frozenset()
    render: frozenset[str] = frozenset()
    cosmetic: frozenset[str] = frozenset()

    @property
    def changed(self) -> bool:
        return bool(self.scope or self.render or self.cosmetic)

    @property
    def requires_refetch(self) -> bool:
        """Bodies or the item set may differ, so stored versions cannot be trusted."""

        return bool(self.scope or self.render)

    @property
    def requires_reconcile(self) -> bool:
        """Items may have left the scope and must be tombstoned."""

        return bool(self.scope)


def diff_configs(old: ConnectorConfig, new: ConnectorConfig) -> ConfigDiff:
    """Compare two configurations of the same connector."""

    if type(old) is not type(new):
        raise ConfigError("cannot diff configurations of different connectors")
    changed: dict[FieldClass, set[str]] = {kind: set() for kind in FieldClass}
    for name, kind in field_classes(type(old)).items():
        if _dump(getattr(old, name)) != _dump(getattr(new, name)):
            changed[kind].add(name)
    return ConfigDiff(
        scope=frozenset(changed[FieldClass.SCOPE]),
        render=frozenset(changed[FieldClass.RENDER]),
        cosmetic=frozenset(changed[FieldClass.COSMETIC]),
    )


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value
