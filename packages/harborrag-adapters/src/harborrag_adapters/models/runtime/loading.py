from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

_MODEL_SECTIONS = frozenset({"chat", "embed", "rerank"})


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses a mapping key written twice."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def load_config_document(path: str | Path) -> Mapping[str, Any]:
    """Load one YAML or JSON configuration document.

    A duplicate key is refused. A catalog with a ``chat``, ``embed`` or ``rerank``
    section may hold only those sections at the top level, so a misspelled section
    fails instead of turning that family off.
    """

    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        raw = _load_yaml(text)
    elif suffix == ".json":
        raw = json.loads(text, object_pairs_hook=_unique_json_object)
    else:
        raise ValueError("configuration file must use .yaml, .yml, or .json")
    if not isinstance(raw, Mapping):
        raise ValueError("configuration document must be a mapping")
    if _MODEL_SECTIONS.intersection(raw):
        unknown = sorted(str(key) for key in raw if key not in _MODEL_SECTIONS)
        if unknown:
            raise ValueError(
                "model configuration has unknown top-level key(s): " + ", ".join(unknown)
            )
    return raw


def _load_yaml(text: str) -> object:
    loader = _UniqueKeyLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"configuration document has duplicate key {key!r}")
        result[key] = value
    return result


def prepare_config_section(
    document: Mapping[str, Any],
    *,
    section: str,
    overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Extract a model-family section and layer explicit overrides on it."""

    raw = deepcopy(dict(document))
    base = raw.get(section, raw)
    if not isinstance(base, Mapping):
        raise ValueError(f"{section} configuration must be a mapping")
    merged = deepcopy(dict(base))
    if overrides:
        merged = merge_config_mappings(merged, overrides)
    return merged


def merge_config_mappings(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge configuration mappings without mutating either input."""

    result = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(result.get(key), Mapping) and isinstance(value, Mapping):
            result[key] = merge_config_mappings(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result
