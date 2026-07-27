from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

_MODEL_SECTIONS = frozenset({"chat", "embed", "rerank"})


def load_config_document(path: str | Path) -> Mapping[str, Any]:
    """Load one YAML or JSON configuration document."""

    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        raw = yaml.safe_load(text)
    elif suffix == ".json":
        raw = json.loads(text)
    else:
        raise ValueError("configuration file must use .yaml, .yml, or .json")
    if not isinstance(raw, Mapping):
        raise ValueError("configuration document must be a mapping")
    return raw


def prepare_config_section(
    document: Mapping[str, Any],
    *,
    section: str,
    profile: str | None,
    overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Extract and layer a model-family section, profile, and explicit overrides."""

    raw = deepcopy(dict(document))
    base = raw.get(section, raw)
    if not isinstance(base, Mapping):
        raise ValueError(f"{section} configuration must be a mapping")
    merged = deepcopy(dict(base))
    if profile:
        profiles = raw.get("profiles", {})
        if not isinstance(profiles, Mapping) or profile not in profiles:
            raise ValueError(f"unknown configuration profile: {profile}")
        selected = profiles[profile]
        if isinstance(selected, Mapping) and section in selected:
            selected = selected[section]
        elif isinstance(selected, Mapping) and _MODEL_SECTIONS.intersection(selected):
            selected = {}
        if not isinstance(selected, Mapping):
            raise ValueError(f"profile {profile!r} must be a mapping")
        merged = merge_config_mappings(merged, selected)
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
