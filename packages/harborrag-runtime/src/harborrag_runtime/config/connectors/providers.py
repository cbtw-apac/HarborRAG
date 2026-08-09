from __future__ import annotations

from collections.abc import Callable, Mapping
from inspect import signature
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from harborrag_adapters.connectors import connector_registry
from harborrag_adapters.connectors.exceptions import ConnectorNotFoundError

type ConnectorConfigFactory = Callable[..., object]

SECRET_CONFIG_FIELDS = frozenset({"access_token", "client_secret", "token"})
PYTHON_ONLY_CONFIG_FIELDS = frozenset(
    {"custom_parsers", "process_attachment_callback", "process_file_callback"}
)


def canonical_provider_name(provider: str) -> str:
    """Normalize a provider name or alias to its canonical name."""
    normalized = provider.strip()
    try:
        return connector_registry.canonical_name(normalized)
    except ConnectorNotFoundError:
        return normalized


def config_factory(provider: str) -> ConnectorConfigFactory | None:
    """Return the provider config constructor, or ``None`` if unsupported."""
    try:
        factory = connector_registry.get_definition(provider).config_factory
    except ConnectorNotFoundError:
        return None
    return factory


def config_field_names(factory: ConnectorConfigFactory) -> set[str]:
    """Return keyword fields accepted by a provider config dataclass."""
    return set(signature(factory).parameters)


def coerce_config_values(
    factory: ConnectorConfigFactory,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Coerce declarative scalar and collection values to provider field types.

    Environment variables are always strings, and quoted YAML scalars are
    strings too. Dataclass annotations are not enforced at runtime, so values
    such as ``"false"`` would otherwise remain truthy strings. Keep coercion
    deliberately limited to the JSON-like types supported by connector YAML.
    """

    annotations = get_type_hints(factory)
    return {
        name: _coerce_config_value(name, value, annotations.get(name))
        for name, value in values.items()
    }


def validate_literal_collection_shapes(
    factory: ConnectorConfigFactory,
    values: Mapping[str, Any],
) -> None:
    """Reject scalar YAML values for provider collection fields.

    Environment variables intentionally support comma-separated collection
    values. Literal YAML settings do not need that ambiguity and must use a
    sequence, so a typo such as ``allowed_extensions: md`` fails while loading
    the catalog instead of being silently split much later during construction.
    """

    annotations = get_type_hints(factory)
    for name, value in values.items():
        annotation = annotations.get(name)
        if annotation is None or value is None:
            continue
        collection = next(
            (
                candidate
                for candidate in _annotation_candidates(annotation)
                if get_origin(candidate) in {list, set, tuple, frozenset}
            ),
            None,
        )
        if collection is None:
            continue
        if isinstance(value, str) or not isinstance(
            value,
            (list, set, tuple, frozenset),
        ):
            raise ValueError(f"{name} must be a list")
        _coerce_collection(name, value, collection)


def _coerce_config_value(name: str, value: Any, annotation: Any) -> Any:
    if annotation is None or value is None:
        return value
    candidates = _annotation_candidates(annotation)
    if bool in candidates:
        return _coerce_bool(name, value)
    if int in candidates and float not in candidates:
        return _coerce_int(name, value)
    if float in candidates:
        return _coerce_float(name, value)

    collection = next(
        (
            candidate
            for candidate in candidates
            if get_origin(candidate) in {list, set, tuple, frozenset}
        ),
        None,
    )
    if collection is not None:
        return _coerce_collection(name, value, collection)
    return value


def _annotation_candidates(annotation: Any) -> tuple[Any, ...]:
    origin = get_origin(annotation)
    if origin in {UnionType, Union}:
        return tuple(value for value in get_args(annotation) if value is not type(None))
    return (annotation,)


def _coerce_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean")


def _coerce_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as error:
            raise ValueError(f"{name} must be an integer") from error
    raise ValueError(f"{name} must be an integer")


def _coerce_float(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as error:
            raise ValueError(f"{name} must be a number") from error
    raise ValueError(f"{name} must be a number")


def _coerce_collection(name: str, value: Any, annotation: Any) -> Any:
    origin = get_origin(annotation)
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, set, tuple, frozenset)):
        items = list(value)
    else:
        raise ValueError(f"{name} must be a collection")

    element_types = tuple(value for value in get_args(annotation) if value is not Ellipsis)
    if element_types and all(element_type is str for element_type in element_types):
        if any(not isinstance(item, str) for item in items):
            raise ValueError(f"{name} values must be strings")
    if origin is set:
        return set(items)
    if origin is frozenset:
        return frozenset(items)
    if origin is tuple:
        return tuple(items)
    return items


def supported_provider_names() -> list[str]:
    """Return canonical provider names in deterministic display order."""
    return [
        name
        for name in connector_registry.canonical_names()
        if connector_registry.get_definition(name).config_factory is not None
    ]
