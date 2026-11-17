"""Read and write the settings file: ``config/settings.yaml``, import files and exports.

Shape: dotted or nested keys; every top-level entry other than ``version``,
``projects`` and ``connections`` is tenant level; ``projects: {<project_id>: {...}}``
and ``connections: {"<project_id>/<connection_id>": {...}}`` hold the other levels.
Keys and values are checked against the registry later, by ``plan_import``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from harborrag_core.config_store import SettingsLevel
from harborrag_core.config_store.documents import SettingsDocument
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_runtime.config.errors import ConfigurationError
from harborrag_runtime.config.loading import read_yaml_file

SETTINGS_FILE_VERSION = 1
_SECTIONS = frozenset({"version", "projects", "connections"})


class SettingsDocumentError(ConfigurationError):
    """The settings file is not shaped as documented.

    Args:
        path: File that failed.
        level: Level label the problem is under, when known.
        key: Offending key, when there is one.
        issue: What is wrong, without values.
    """

    def __init__(self, path: Path, level: str | None, key: str | None, issue: str) -> None:
        parts = [f"settings file {path}:"]
        if key:
            parts.append(f"key {key}")
        if level:
            parts.append(f"(level {level})")
        super().__init__(" ".join([*parts, issue]))
        self.path = path
        self.level = level
        self.key = key
        self.issue = issue


def _flatten(
    path: Path, level: str | None, body: Mapping[object, object], prefix: str = ""
) -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in body.items():
        if not isinstance(key, str):
            raise SettingsDocumentError(path, level, str(key), "key must be a string")
        dotted = f"{prefix}{key}"
        for name, item in (
            _flatten(path, level, value, f"{dotted}.").items()
            if isinstance(value, Mapping)
            else [(dotted, value)]
        ):
            if name in flat:
                raise SettingsDocumentError(path, level, name, "is set twice")
            flat[name] = item
    return flat


def _section(
    path: Path, raw: Mapping[str, object], name: str, kind: str
) -> dict[str, dict[str, object]]:
    section = raw.get(name)
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise SettingsDocumentError(path, None, name, "must be a mapping")
    levels: dict[str, dict[str, object]] = {}
    for ref, body in section.items():
        try:
            label = str(SettingsLevel.parse(f"{kind}:{ref}"))
        except HarborValidationError as error:
            raise SettingsDocumentError(path, None, str(ref), "is not a valid level") from error
        if not isinstance(body, Mapping):
            raise SettingsDocumentError(path, label, str(ref), "must be a mapping")
        levels[label] = _flatten(path, label, body)
    return levels


def load_settings_document(path: Path) -> SettingsDocument:
    """Read ``path`` into a document; an empty file is an empty document.

    Args:
        path: YAML settings file.

    Returns:
        Overrides by level label and dotted key; empty levels are dropped.

    Raises:
        SettingsDocumentError: The file is unreadable, has a duplicate key or is
            not shaped as documented; the message names the path and the key.
    """
    try:
        _, raw = read_yaml_file(path, label="settings file")
    except ConfigurationError as error:
        raise SettingsDocumentError(path, None, None, str(error)) from error
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise SettingsDocumentError(path, None, None, "must be a mapping")
    version = raw.get("version", SETTINGS_FILE_VERSION)
    if type(version) is not int or version != SETTINGS_FILE_VERSION:
        raise SettingsDocumentError(path, None, "version", f"must be {SETTINGS_FILE_VERSION}")
    tenant = _flatten(path, "tenant", {k: v for k, v in raw.items() if k not in _SECTIONS})
    levels = {
        "tenant": tenant,
        **_section(path, raw, "projects", "project"),
        **_section(path, raw, "connections", "connection"),
    }
    return SettingsDocument({label: body for label, body in levels.items() if body})


def dump_settings_document(document: SettingsDocument) -> str:
    """Return ``document`` as YAML in the file shape: tenant keys first, keys sorted."""
    out: dict[str, object] = dict(sorted(document.levels.get("tenant", {}).items()))
    for kind, section in (("project", "projects"), ("connection", "connections")):
        prefix = f"{kind}:"
        bodies = {
            label.removeprefix(prefix): dict(sorted(body.items()))
            for label, body in sorted(document.levels.items())
            if label.startswith(prefix)
        }
        if bodies:
            out[section] = bodies
    return yaml.safe_dump(out, sort_keys=False, default_flow_style=False)
