"""Project scaffolding behind ``harborrag init``."""

from .providers import DEFAULT_PROVIDER, PRESETS, ProviderPreset
from .render import (
    COMPOSE_FALLBACK,
    COMPOSE_FILE,
    ENV_FALLBACK,
    ENV_FILE,
    FORCE_HINT,
    SERVICE_PORTS,
    STATE_DIRECTORY,
    InitOptions,
    ScaffoldExistsError,
    build_scaffold,
    existing_scaffold_files,
    write_scaffold,
)
from .sources import DEFAULT_SOURCE, SOURCES, SourcePreset, SourceVariable, parse_sources

__all__ = [
    "DEFAULT_PROVIDER",
    "DEFAULT_SOURCE",
    "SOURCES",
    "SourcePreset",
    "SourceVariable",
    "parse_sources",
    "COMPOSE_FALLBACK",
    "COMPOSE_FILE",
    "ENV_FALLBACK",
    "ENV_FILE",
    "FORCE_HINT",
    "PRESETS",
    "SERVICE_PORTS",
    "STATE_DIRECTORY",
    "InitOptions",
    "ProviderPreset",
    "ScaffoldExistsError",
    "build_scaffold",
    "existing_scaffold_files",
    "write_scaffold",
]
