from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict


class LogicalModelView(Protocol):
    """Expose model aliases and fallback references to shared configuration logic."""

    @property
    def aliases(self) -> frozenset[str]:
        """Return alternate logical names."""

        ...

    @property
    def fallbacks(self) -> tuple[str, ...]:
        """Return ordered logical fallback references."""

        ...


class NamedDeployment(Protocol):
    """Expose the stable deployment name required for uniqueness checks."""

    @property
    def name(self) -> str:
        """Return the deployment name."""

        ...


class LogicalModelConfig(BaseModel):
    """Define aliases and fallbacks common to every logical model family."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aliases: frozenset[str] = frozenset()
    fallbacks: tuple[str, ...] = ()


def normalize_single_deployment_shorthand(raw: Any, *, provider_fields: frozenset[str]) -> Any:
    """Expand compact logical-model entries into explicit deployment collections."""

    if not isinstance(raw, Mapping):
        return raw
    data = deepcopy(dict(raw))
    models = data.get("models")
    if not isinstance(models, Mapping):
        return data
    normalized: dict[str, Any] = {}
    for logical_name, entry in models.items():
        if isinstance(entry, Mapping) and "provider" in entry and "deployments" not in entry:
            entry_data = dict(entry)
            deployment_data = {
                key: entry_data.pop(key) for key in tuple(entry_data) if key in provider_fields
            }
            deployment_data.setdefault("name", logical_name)
            normalized[str(logical_name)] = {
                **entry_data,
                "deployments": [deployment_data],
            }
        else:
            normalized[str(logical_name)] = entry
    data["models"] = normalized
    return data


def validate_logical_model_references(
    models: Mapping[str, LogicalModelView],
    *,
    default_model: str,
    family_name: str,
) -> dict[str, str]:
    """Validate a default, unique aliases, and known fallback references."""

    aliases: dict[str, str] = {}
    for name, model in models.items():
        for alias in model.aliases:
            if alias in aliases or alias in models:
                raise ValueError(f"duplicate or conflicting {family_name} model alias: {alias}")
            aliases[alias] = name
    if default_model not in models and default_model not in aliases:
        raise ValueError(f"default_model {default_model!r} is not configured")
    for name, model in models.items():
        for fallback in model.fallbacks:
            if fallback not in models and fallback not in aliases:
                raise ValueError(f"fallback {fallback!r} referenced by {name!r} is not configured")
    _validate_acyclic_fallbacks(models, aliases, family_name=family_name)
    return aliases


def resolve_logical_model(models: Mapping[str, LogicalModelView], name: str) -> str | None:
    """Resolve a canonical logical model name or alias."""

    if name in models:
        return name
    return next(
        (logical_name for logical_name, model in models.items() if name in model.aliases),
        None,
    )


def logical_fallback_chain(models: Mapping[str, LogicalModelView], start: str) -> tuple[str, ...]:
    """Resolve a logical model and recursively visit each configured fallback once."""

    result: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        resolved = resolve_logical_model(models, name)
        if resolved is None or resolved in visiting or resolved in result:
            return
        visiting.add(resolved)
        result.append(resolved)
        for fallback in models[resolved].fallbacks:
            visit(fallback)
        visiting.remove(resolved)

    visit(start)
    return tuple(result)


def _validate_acyclic_fallbacks(
    models: Mapping[str, LogicalModelView],
    aliases: Mapping[str, str],
    *,
    family_name: str,
) -> None:
    visited: set[str] = set()
    visiting: list[str] = []

    def visit(name: str) -> None:
        if name in visiting:
            cycle = " -> ".join((*visiting[visiting.index(name) :], name))
            raise ValueError(f"circular {family_name} fallback chain: {cycle}")
        if name in visited:
            return
        visiting.append(name)
        for fallback in models[name].fallbacks:
            visit(aliases.get(fallback, fallback))
        visiting.pop()
        visited.add(name)

    for logical_name in models:
        visit(logical_name)


def validate_unique_deployments(
    deployments: Sequence[NamedDeployment], *, family_name: str
) -> None:
    """Require at least one uniquely named deployment for a logical model."""

    if not deployments:
        raise ValueError(f"logical {family_name} model must contain at least one deployment")
    names = [deployment.name for deployment in deployments]
    if len(names) != len(set(names)):
        raise ValueError(f"deployment names must be unique within a logical {family_name} model")
