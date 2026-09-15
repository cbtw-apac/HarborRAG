"""Render the project templates and write them to disk."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from string import Template
from urllib.parse import urlparse

from .providers import PRESETS, ProviderPreset
from .sources import DEFAULT_SOURCE, SOURCES

_TEMPLATES = resources.files("harborrag_app.scaffold").joinpath("templates")
ENV_FILE = ".env"
ENV_FALLBACK = ".env.new"
STATE_DIRECTORY = ".harborrag"
_FILES: tuple[tuple[str, str], ...] = (
    ("harborrag.yaml", "harborrag.yaml.tmpl"),
    (ENV_FILE, "dotenv.tmpl"),
    (".gitignore", "gitignore.tmpl"),
    ("docker-compose.yml", "compose.yml.tmpl"),
    ("config/connectors.yaml", "connectors.yaml.tmpl"),
    ("config/models.yaml", "models.yaml.tmpl"),
    ("config/parsers.yaml", "parsers.yaml.tmpl"),
    ("config/temporal.yaml", "temporal.yaml.tmpl"),
)
_DEPLOYMENT_INDENT = " " * 10
_CAPABILITY_INDENT = " " * 12
# Host ports docker-compose.yml publishes at offset 0, with the service each belongs to.
SERVICE_PORTS: tuple[tuple[str, int, str], ...] = (
    ("qdrant_http_port", 6333, "qdrant"),
    ("qdrant_grpc_port", 6334, "qdrant"),
    ("falkordb_port", 6379, "falkordb"),
    ("minio_api_port", 9000, "minio"),
    ("minio_console_port", 9001, "minio"),
)
_QDRANT_REST_ONLY = (
    "# Qdrant is not on its default port; the client cannot derive the gRPC port from the\n"
    "# URL, so it talks REST. Remove this line if you map 6334 back to the default.\n"
    "HARBORRAG_QDRANT_PREFER_GRPC=false\n"
)


class ScaffoldTemplate(Template):
    """``@{name}`` placeholders, so literal ``${ENV}`` references survive rendering."""

    delimiter = "@"


class ScaffoldExistsError(FileExistsError):
    """The target already holds scaffold files and ``--force`` was not given."""


@dataclass(frozen=True, slots=True)
class InitOptions:
    provider: str
    chat_model: str
    embed_model: str
    api_key: str
    api_base: str | None
    source_path: str
    embed_dimensions: int | None = None
    ports_offset: int = 0
    sources: tuple[str, ...] = (DEFAULT_SOURCE,)
    # Answers for SourceVariable prompts, keyed by variable name.
    source_values: Mapping[str, str] = field(default_factory=dict)

    @property
    def connector_names(self) -> tuple[str, ...]:
        return tuple(SOURCES[key].connector_name for key in self.sources)

    @property
    def has_local_source(self) -> bool:
        return "local" in self.sources

    @property
    def preset(self) -> ProviderPreset:
        return PRESETS[self.provider]

    @property
    def resolved_embed_dimensions(self) -> int:
        """Vector size the runtime indexes at; the preset default unless overridden."""

        dimensions = self.embed_dimensions or self.preset.embed_dimensions
        if dimensions is None:
            raise ValueError(f"provider {self.provider!r} requires --embed-dimensions")
        return dimensions


def build_scaffold(options: InitOptions, *, created_at: str) -> dict[str, str]:
    """Render every project file; keys are paths relative to the project root."""

    values = _values(options, created_at=created_at)
    return {
        relative: ScaffoldTemplate(_read(template)).substitute(values)
        for relative, template in _FILES
    }


def existing_scaffold_files(root: Path) -> list[str]:
    """Scaffold paths already present under ``root`` (relative, in scaffold order)."""

    return [relative for relative, _template in _FILES if (root / relative).exists()]


def write_scaffold(root: Path, files: Mapping[str, str], *, force: bool) -> tuple[Path, ...]:
    """Write the rendered files under ``root``; never clobber an existing ``.env``."""

    existing = [relative for relative in files if (root / relative).exists()]
    if existing and not force:
        raise ScaffoldExistsError(
            f"{root} already contains {', '.join(sorted(existing))}; pass --force to overwrite"
        )
    written: list[Path] = []
    for relative, content in files.items():
        target = root / relative
        if relative == ENV_FILE and target.exists():
            # Secrets the user already filled in must survive a re-run.
            target = root / ENV_FALLBACK
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if relative == ENV_FILE:
            target.chmod(0o600)
        written.append(target)
    # The SQLite control database lives here; aiosqlite does not create parent folders.
    (root / STATE_DIRECTORY).mkdir(exist_ok=True)
    return tuple(written)


def _values(options: InitOptions, *, created_at: str) -> dict[str, str]:
    preset = options.preset
    credential_lines = [f"{preset.credential_variable}={options.api_key}"]
    deployment_extra: list[str] = []
    security_extra = ""
    if preset.requires_api_base:
        if not options.api_base or not preset.api_base_variable:
            raise ValueError(f"provider {preset.key!r} requires --api-base")
        credential_lines.append(f"{preset.api_base_variable}={options.api_base}")
        deployment_extra.append(f"api_base: ${{{preset.api_base_variable}}}")
        host = urlparse(options.api_base).hostname or ""
        security_extra = f"    allowed_base_url_hosts: [{host}]\n"
    if preset.api_version:
        deployment_extra.append(f'api_version: "{preset.api_version}"')
    chat_extra = list(deployment_extra)
    embed_extra = list(deployment_extra)
    if preset.provider == "azure_openai":
        chat_extra.append(f"deployment_name: {options.chat_model}")
        embed_extra.append(f"deployment_name: {options.embed_model}")
    dimensions = options.resolved_embed_dimensions
    embed_extra.append(f"expected_dimensions: {dimensions}")
    # The runtime encodes at expected_dimensions and tags each request with its purpose,
    # so the deployment must advertise both or every document fails at EncodeChunks.
    embed_capability_extra = f"{_CAPABILITY_INDENT}default_dimensions: {dimensions}\n"
    values = {
        "created_at": created_at,
        "provider": preset.provider,
        "provider_label": preset.label,
        "model_prefix": preset.model_prefix,
        "chat_model": options.chat_model,
        "embed_model": options.embed_model,
        "credential_variable": preset.credential_variable,
        "credential_lines": "\n".join(credential_lines),
        "source_path": options.source_path,
        "encryption_key": secrets.token_hex(32),
        "object_store_secret": secrets.token_urlsafe(24),
        "security_extra": security_extra,
        "chat_extra": _indented(chat_extra),
        "embed_extra": _indented(embed_extra),
        "embed_capability_extra": embed_capability_extra,
        "qdrant_transport_lines": _QDRANT_REST_ONLY if options.ports_offset else "",
    }
    for name, base, _service in SERVICE_PORTS:
        values[name] = str(base + options.ports_offset)
    values.update(_source_values(options))
    return values


def _source_values(options: InitOptions) -> dict[str, str]:
    unknown = [key for key in options.sources if key not in SOURCES]
    if unknown:
        raise ValueError(
            f"unknown data source {', '.join(unknown)}; choose from {', '.join(SOURCES)}"
        )
    blocks: list[str] = []
    env_sections: list[str] = []
    for key in options.sources:
        preset = SOURCES[key]
        yaml_values = {
            variable.name: options.source_values.get(variable.name, "") or variable.default
            for variable in preset.variables
            if variable.yaml_only
        }
        if "JIRA_PROJECT_KEYS" in yaml_values:
            keys = [item.strip() for item in yaml_values["JIRA_PROJECT_KEYS"].split(",")]
            yaml_values["JIRA_PROJECT_KEYS"] = ", ".join(item for item in keys if item)
        blocks.append(ScaffoldTemplate(preset.block).substitute(yaml_values).rstrip("\n"))
        env_lines = [
            f"{variable.name}={options.source_values.get(variable.name, '') or variable.default}"
            for variable in preset.variables
            if not variable.yaml_only
        ]
        if env_lines:
            env_sections.append(
                f"# {preset.label} ({preset.connector_name} connector)\n" + "\n".join(env_lines)
            )
    local_lines = (
        "# Folder ingested by the `workspace` connector (relative to this directory)\n"
        f"LOCAL_SOURCE_PATH={options.source_path}\n\n"
        if options.has_local_source
        else ""
    )
    return {
        "connector_blocks": "\n\n".join(blocks) + "\n",
        "local_source_lines": local_lines,
        "connector_env_lines": ("\n" + "\n\n".join(env_sections) + "\n") if env_sections else "",
    }


def _indented(lines: list[str]) -> str:
    return "".join(f"{_DEPLOYMENT_INDENT}{line}\n" for line in lines)


def _read(name: str) -> str:
    return _TEMPLATES.joinpath(name).read_text(encoding="utf-8")


__all__ = [
    "ENV_FALLBACK",
    "ENV_FILE",
    "SERVICE_PORTS",
    "STATE_DIRECTORY",
    "InitOptions",
    "ScaffoldExistsError",
    "build_scaffold",
    "existing_scaffold_files",
    "write_scaffold",
]
