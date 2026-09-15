"""Locate and activate a HarborRAG project directory.

A project is any directory that holds ``harborrag.yaml`` -- the same file
``HarborRAG.from_config`` reads. Activation makes the process behave as if it had been
started from that directory: every runtime default (catalog paths, the SQLite control DB
URL, ``LOCAL_SOURCE_PATH``) is CWD-relative today, and one ``chdir`` keeps that contract
without threading a root through every loader.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("harborrag.app.cli.project")

# The project main() activated for this process, so later code (doctor) reports it instead
# of re-running discovery from the new CWD -- which would re-apply the safety rule to a
# directory the user already opted into with --project.
_ACTIVE: Project | None = None

PROJECT_FILE = "harborrag.yaml"
# A repository checkout carries no harborrag.yaml but does ship catalogs under config/.
LEGACY_CONFIG_MARKER = Path("config") / "connectors.yaml"
PROJECT_ENV_VAR = "HARBORRAG_PROJECT"
ENV_FILE = ".env"
_SETTINGS_PREFIX = "HARBORRAG_"
_PATH_KEYS = frozenset(
    {
        "temporal_config_path",
        "connector_config_path",
        "parser_config_path",
        "model_config_path",
    }
)


class ProjectError(Exception):
    """Base class for project discovery and configuration failures."""


class ProjectNotFoundError(ProjectError):
    """An explicitly named project directory has no marker file."""


class ProjectConfigurationError(ProjectError):
    """The marker file exists but is not a usable mapping."""


class UnsafeProjectError(ProjectError):
    """A walked-to marker lives in a directory another user could have written."""


@dataclass(frozen=True, slots=True)
class Remedy:
    """The advice appended when a walked-to directory is refused."""

    unowned: str
    permissions: str


@dataclass(frozen=True, slots=True)
class Project:
    """A discovered project: its root and the ``runtime:`` section of the marker."""

    root: Path
    runtime: dict[str, Any]

    @property
    def marker(self) -> Path:
        return self.root / PROJECT_FILE

    @property
    def env_file(self) -> Path:
        return self.root / ENV_FILE


def find_project(start: Path | None = None, *, explicit: str | None = None) -> Project | None:
    """Return the project named explicitly, by env var, or found by walking up from CWD."""

    candidate = explicit or os.environ.get(PROJECT_ENV_VAR)
    if candidate:
        root = Path(candidate).expanduser().resolve()
        if not (root / PROJECT_FILE).is_file():
            raise ProjectNotFoundError(f"{root} does not contain {PROJECT_FILE}")
        return _load(root)
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if (directory / PROJECT_FILE).is_file():
            _ensure_safe(directory)
            return _load(directory)
    return None


def activate_project(project: Project) -> None:
    """chdir into the project, load its .env, then seed runtime defaults into the env.

    Precedence, highest first: process environment, project ``.env``, marker
    ``runtime:`` values. ``override=False`` and ``setdefault`` implement exactly that order.
    """

    global _ACTIVE  # noqa: PLW0603 - one activation per process, by design
    os.chdir(project.root)
    _ACTIVE = project
    logger.debug("Activated project %s", project.root)
    if project.env_file.is_file():
        from dotenv import load_dotenv

        load_dotenv(project.env_file, override=False)
    for key, value in project.runtime.items():
        name = f"{_SETTINGS_PREFIX}{str(key).upper()}"
        if name in os.environ:
            continue
        if value is None:
            # `connector_config_path:` with no value parses as None; exporting the string
            # "None" (or <root>/None for a path key) suppresses the real default and fails
            # later in a loader that cannot name the cause.
            continue
        if key in _PATH_KEYS:
            value = str((project.root / str(value)).resolve())
        os.environ[name] = _env_value(value)


def active_project() -> Project | None:
    """The project activated earlier in this process, if any."""

    return _ACTIVE


def activate_from_argv(args: Sequence[str]) -> Project | None:
    """Discover and activate the project selected by ``--project``/env/CWD, if any."""

    project = find_project(explicit=project_option_from_argv(args))
    if project is not None:
        activate_project(project)
    return project


def find_legacy_checkout(start: Path | None = None) -> Path | None:
    """Return the enclosing repository checkout whose ``config/`` supplies catalogs.

    Resolved by the same upward walk as :data:`PROJECT_FILE`, and subject to the same
    ownership rule. Checking ``config/connectors.yaml`` against the raw CWD instead would
    make the CLI work at a checkout's root and fail one directory below it.
    """

    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if (directory / LEGACY_CONFIG_MARKER).is_file():
            _ensure_safe(directory, str(LEGACY_CONFIG_MARKER), _CHECKOUT_REMEDY)
            return directory
    return None


def activate_legacy_checkout(start: Path | None = None) -> Path | None:
    """chdir into the enclosing checkout so CWD-relative catalog defaults resolve.

    Catalog paths are CWD-relative (see :func:`activate_project`), so locating a checkout
    in an ancestor is only useful if the process also moves there.
    """

    root = find_legacy_checkout(start)
    if root is not None:
        os.chdir(root)
        logger.debug("Activated repository checkout %s", root)
    return root


def project_option_from_argv(args: Sequence[str]) -> str | None:
    """Read ``--project PATH`` or ``--project=PATH`` before Click parses the command line."""

    for index, arg in enumerate(args):
        if arg == "--project" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--project="):
            return arg.split("=", 1)[1]
    return None


# How to accept a rejected directory deliberately. A marker can be named with --project;
# a checkout cannot, because --project insists on a harborrag.yaml, so cd is the only way in.
_MARKER_REMEDY = Remedy(
    unowned="cd into it or pass --project to use it deliberately",
    permissions="fix its permissions or pass --project to use it deliberately",
)
_CHECKOUT_REMEDY = Remedy(
    unowned="cd into it to use it deliberately",
    permissions="fix its permissions or cd into it to use it deliberately",
)


def _ensure_safe(
    directory: Path, marker: str = PROJECT_FILE, remedy: Remedy = _MARKER_REMEDY
) -> None:
    """Refuse a walked-to marker unless the current user controls its directory.

    The walk climbs to ``/``, so without this a co-tenant who can write ``/tmp`` (or any
    shared ancestor) could plant ``harborrag.yaml`` + ``.env`` and redirect every command a
    user runs from a subdirectory -- model endpoint, stores, source folder -- the same class
    of problem git closed with ``safe.directory``. An explicit ``--project`` /
    ``HARBORRAG_PROJECT`` is the user's own choice and skips this check.
    """

    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:  # pragma: no cover - POSIX ownership is not modelled on Windows
        return
    info = directory.stat()
    if info.st_uid != geteuid():
        raise UnsafeProjectError(
            f"found {marker} in {directory}, which is not owned by you; {remedy.unowned}"
        )
    if info.st_mode & stat.S_IWOTH:
        raise UnsafeProjectError(
            f"found {marker} in {directory}, which is world-writable; {remedy.permissions}"
        )


def _load(root: Path) -> Project:
    marker = root / PROJECT_FILE
    try:
        raw = yaml.safe_load(marker.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        # main() converts ProjectError into a one-line message; anything else reaches the
        # interpreter as a traceback, which a typo in harborrag.yaml does not warrant.
        raise ProjectConfigurationError(f"{marker} could not be read: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectConfigurationError(f"{marker} must contain a mapping")
    runtime = raw.get("runtime") or {}
    if not isinstance(runtime, dict):
        raise ProjectConfigurationError(f"{marker}: 'runtime' must be a mapping")
    return Project(root=root, runtime={str(key): value for key, value in runtime.items()})


def _env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


__all__ = [
    "ENV_FILE",
    "LEGACY_CONFIG_MARKER",
    "PROJECT_ENV_VAR",
    "PROJECT_FILE",
    "Project",
    "ProjectConfigurationError",
    "ProjectError",
    "ProjectNotFoundError",
    "UnsafeProjectError",
    "activate_from_argv",
    "activate_legacy_checkout",
    "active_project",
    "activate_project",
    "find_legacy_checkout",
    "find_project",
    "project_option_from_argv",
]
