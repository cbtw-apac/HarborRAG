"""Local file source type: a directory or file on the worker's file system."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .common import NoCredentials, NoTuning
from .fields import StrictModel, widget
from .spec import AuthMethodSpec, Capabilities, FieldIssue, Setup, SourceTypeSpec

LOCAL_SOURCE_ROOT_VARIABLE = "HARBORRAG_LOCAL_SOURCE_ROOT"
LOCAL_SOURCE_PATH_VARIABLE = "LOCAL_SOURCE_PATH"


def local_source_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """The directory local connections must stay inside, when the install sets one.

    ``HARBORRAG_LOCAL_SOURCE_ROOT`` wins; otherwise the ``LOCAL_SOURCE_PATH``
    directory the compose files configure.
    """
    env = os.environ if environ is None else environ
    value = env.get(LOCAL_SOURCE_ROOT_VARIABLE) or env.get(LOCAL_SOURCE_PATH_VARIABLE)
    return Path(value).expanduser().resolve() if value else None


def local_source_issues(config: Mapping[str, Any]) -> list[FieldIssue]:
    """Refuse a ``source_path`` outside the configured root, or relative while one is set."""
    root = local_source_root()
    if root is None or not isinstance(config.get("source_path"), str):
        return []
    path = Path(config["source_path"]).expanduser()
    if not path.is_absolute():
        return [FieldIssue("config.source_path", "must be an absolute path")]
    if not path.resolve().is_relative_to(root):
        return [FieldIssue("config.source_path", f"must be inside {root}")]
    return []


def local_source_api_issues(config: Mapping[str, Any]) -> list[FieldIssue]:
    """Refuse an API write of a local connection while the install sets no root.

    Without a root, any path the workers can read would be accepted, such as
    their own secrets; the CLI and ``connectors.yaml`` stay open to operators.
    """
    if local_source_root() is not None:
        return []
    return [
        FieldIssue(
            "config.source_path",
            f"set {LOCAL_SOURCE_ROOT_VARIABLE} on the API and workers to add local "
            "connections through the API",
        )
    ]


class LocalFileSyncConfig(StrictModel):
    """What to sync from the file system."""

    source_path: str = Field(
        min_length=1,
        description="Absolute path as the worker sees it, inside the install's local source root",
        examples=["/data/sources"],
        json_schema_extra=widget("path"),
    )
    allowed_extensions: list[str] = Field(
        default_factory=list,
        description=(
            "File types to keep, such as pdf; left out, common document and image types; "
            "[] keeps every type"
        ),
    )
    excluded_extensions: list[str] = Field(default_factory=list, description="File types to drop")
    include_paths: list[str] = Field(
        default_factory=list, description="Paths under source_path to read; empty reads all"
    )
    exclude_paths: list[str] = Field(default_factory=list, description="Paths to skip")
    include_globs: list[str] = Field(
        default_factory=list, description="Glob patterns to read, such as **/*.pdf"
    )
    exclude_globs: list[str] = Field(default_factory=list, description="Glob patterns to skip")
    excluded_dir_names: list[str] | None = Field(default=None, description="Folders to skip")
    include_hidden: bool = Field(default=False, description="Include hidden files and folders")
    follow_symlinks: bool = Field(default=False, description="Follow links inside the path")
    max_depth: int | None = Field(default=None, ge=0, description="Folder depth limit")
    max_file_size_bytes: int | None = Field(
        default=None, ge=0, description="Largest file to read, in bytes; left out, 20 MiB"
    )
    checksum_mode: Literal["none", "stat", "sha256"] = Field(
        default="stat", description="How file changes are detected"
    )
    fail_on_error: bool = Field(default=False, description="Stop the run on the first item error")


LOCAL_FILE = SourceTypeSpec(
    source_type="local_file",
    name="Local files",
    category="files",
    provider="local",
    auth_methods=(AuthMethodSpec("none", "No authentication", NoCredentials),),
    config_model=LocalFileSyncConfig,
    tuning_model=NoTuning,
    capabilities=Capabilities(incremental=True, attachments=False),
    aliases=("local", "local_files", "filesystem", "files"),
    build_check=False,
    policy=local_source_issues,
    api_policy=local_source_api_issues,
    setups=(
        Setup(
            "folder",
            "Local files",
            "none",
            "Read a folder the workers can see, inside HARBORRAG_LOCAL_SOURCE_ROOT when it is set",
        ),
    ),
)
