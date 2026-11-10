"""Named profiles in the CLI's config file: a default tenant and output format per name.

The file holds one mapping, ``profiles``, from profile name to ``tenant`` and ``output``.
Remote-mode keys (``api_url``, ``api_key``) are refused until v2.1.0. A missing file means
no profiles; a named profile other than ``default`` that the file lacks is an error, so a
typo never runs a command in the wrong tenant. Messages name keys, never values.

Start from: load_profile.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import click
import yaml

from harborrag_app.cli.output import OutputFormat

DEFAULT_CONFIG_PATH = Path("~/.config/harborrag/config.yaml")
DEFAULT_PROFILE = "default"
_PROFILES_KEY = "profiles"
_ALLOWED_KEYS = frozenset({"tenant", "output"})
_REMOTE_KEYS = frozenset({"api_url", "api_key"})
_FORMATS = ", ".join(OutputFormat)


class ProfileError(click.UsageError):
    """The config file or the chosen profile cannot be used."""


@dataclass(frozen=True, slots=True)
class Profile:
    """The defaults one profile sets; ``None`` keeps the built-in default.

    Args:
        tenant: Tenant the commands act in.
        output: Format the commands print.
    """

    tenant: str | None = None
    output: OutputFormat | None = None


def load_profile(path: Path, name: str) -> Profile:
    """Read profile ``name`` from the config file at ``path``.

    Args:
        path: Config file; ``~`` is expanded.
        name: Profile to read.

    Returns:
        The profile's defaults; empty for ``default`` when the file or the profile is missing.

    Raises:
        ProfileError: The file is malformed; the profile is missing and is not ``default``;
            or it holds a remote-mode key, an unknown key or a bad value.
    """
    config = path.expanduser()
    entry = _profiles(config).get(name)
    if entry is None:
        if name == DEFAULT_PROFILE:
            return Profile()
        raise ProfileError(f"profile `{name}` is not in {config}")
    where = f"profile `{name}` in {config}"
    if not isinstance(entry, Mapping):
        raise ProfileError(f"{where} must map keys to values")
    if _REMOTE_KEYS.intersection(entry):
        raise ProfileError("remote mode (api_url) arrives in v2.1.0")
    unknown = sorted(str(key) for key in entry if key not in _ALLOWED_KEYS)
    if unknown:
        raise ProfileError(f"{where} has unknown keys {unknown}; allowed: tenant, output")
    return Profile(
        tenant=_tenant(entry.get("tenant"), where=where),
        output=_output(entry.get("output"), where=where),
    )


def _profiles(config: Path) -> Mapping[object, object]:
    """The file's ``profiles`` mapping; empty when the file does not exist or is empty."""
    try:
        text = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ProfileError(f"cannot read {config}: {exc.strerror}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ProfileError(f"{config} is not valid YAML") from None
    if document is None:
        return {}
    if not isinstance(document, Mapping) or set(document) - {_PROFILES_KEY}:
        raise ProfileError(f"{config} must hold one mapping, `{_PROFILES_KEY}`")
    profiles = document.get(_PROFILES_KEY) or {}
    if not isinstance(profiles, Mapping):
        raise ProfileError(f"`{_PROFILES_KEY}` in {config} must map profile names to settings")
    return profiles


def _tenant(value: object, *, where: str) -> str | None:
    """The profile's tenant; a blank or non-text value is refused."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    raise ProfileError(f"`tenant` of {where} must be a non-empty string")


def _output(value: object, *, where: str) -> OutputFormat | None:
    """The profile's output format; anything but table, json or yaml is refused."""
    if value is None:
        return None
    if isinstance(value, str) and value in set(OutputFormat):
        return OutputFormat(value)
    raise ProfileError(f"`output` of {where} must be one of {_FORMATS}")
