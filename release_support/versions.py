"""Workspace release versions: read them, choose the next one, and write it.

Every function here works on the ``pyproject.toml`` files listed in
:data:`release_support.config.PACKAGES`, editing text in place so formatting
and comments survive:

- **Files** — locate a package's metadata and its ``[project]`` table.
- **Read** — current versions, the lockstep check, and release order.
- **Next version** — major/minor/patch/beta bumps and explicit versions,
  including friendly aliases such as ``2.0.0-alpha``.
- **Write** — the version itself, the PyPI ``Development Status`` classifier,
  and the ``==<version>`` pins between internal packages.
"""

import logging
import re
import sys
import tomllib
from enum import IntEnum
from pathlib import Path

from packaging.version import InvalidVersion, Version

from . import config
from .config import PACKAGES, PRIMARY_PACKAGE, WORKSPACE_PACKAGE
from .metadata import TYPESCRIPT_PACKAGE


def pyproject_path(package_name: str) -> Path:
    """Return the metadata file for one configured release package."""

    try:
        relative_path = PACKAGES[package_name]["pyproject"]
    except KeyError as exc:
        raise ValueError(f"Unknown release package: {package_name}") from exc
    return config.repository_path(relative_path)


def read_source(path: Path) -> str:
    """Read metadata without normalizing its existing line endings."""

    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def project_section_span(source: str, path: Path) -> tuple[int, int]:
    """Return the start and end offsets of the ``[project]`` table body."""

    project_match = re.search(r"(?m)^\[project\]\s*$", source)
    if project_match is None:
        raise ValueError(f"[project] section is missing from {path}")
    next_section = re.search(r"(?m)^\[", source[project_match.end() :])
    section_end = (
        project_match.end() + next_section.start() if next_section is not None else len(source)
    )
    return project_match.end(), section_end


def get_package_version(package_name: str) -> str:
    """Return the PEP 621 version for one configured package."""

    path = pyproject_path(package_name)
    if not path.is_file():
        raise FileNotFoundError(f"Package metadata not found: {path}")
    project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"[project].version is missing from {path}")
    return version


def get_current_version() -> str:
    """Return the public facade version used as release source of truth."""

    return get_package_version(PRIMARY_PACKAGE)


def get_all_package_versions() -> dict[str, str]:
    """Return synchronized versions or stop when workspace versions diverge."""

    versions = {name: get_package_version(name) for name in PACKAGES}
    if len(set(versions.values())) == 1:
        return versions

    logger = logging.getLogger("release")
    logger.error("Version mismatch detected; all release projects must match.")
    for name, version in versions.items():
        logger.error("  %s: %s", name, version)
    raise SystemExit(1)


def get_packages_for_release() -> list[str]:
    """Return publishable packages in dependency order with the facade last."""

    packages = [name for name, package in PACKAGES.items() if package["create_release"]]
    if PRIMARY_PACKAGE in packages:
        packages.remove(PRIMARY_PACKAGE)
        packages.append(PRIMARY_PACKAGE)
    return packages


def assert_release_files_exist() -> None:
    """Fail early when release configuration references stale package paths."""

    missing = [str(pyproject_path(name)) for name in PACKAGES if not pyproject_path(name).is_file()]
    if not TYPESCRIPT_PACKAGE.is_file():
        missing.append(str(TYPESCRIPT_PACKAGE))
    if missing:
        logging.getLogger("release").error(
            "Release configuration references missing package metadata:\n%s",
            "\n".join(f"  - {path}" for path in missing),
        )
        sys.exit(1)


class BumpType(IntEnum):
    """Version bump selected by the operator; the values are the menu numbers."""

    MAJOR = 1
    MINOR = 2
    PATCH = 3
    BETA = 4
    CUSTOM = 5


_CUSTOM_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$")
_FRIENDLY_PRERELEASE = re.compile(
    r"^(?P<release>\d+\.\d+\.\d+)-(?P<phase>alpha|beta|rc)(?:\.(?P<number>\d+))?$",
    re.IGNORECASE,
)
_FRIENDLY_PHASES = {"alpha": "a", "beta": "b", "rc": "rc"}


def _validated_custom_version(custom_version: str | None) -> str:
    if custom_version is not None and _CUSTOM_VERSION.fullmatch(custom_version):
        return custom_version

    friendly = _FRIENDLY_PRERELEASE.fullmatch(custom_version or "")
    if friendly is not None:
        phase = _FRIENDLY_PHASES[friendly.group("phase").casefold()]
        number = friendly.group("number") or "1"
        return f"{friendly.group('release')}{phase}{number}"

    raise ValueError(
        "Custom version must use X.Y.Z, X.Y.ZaN, X.Y.ZbN, X.Y.ZrcN, "
        "or a friendly X.Y.Z-alpha[.N], X.Y.Z-beta[.N], or X.Y.Z-rc[.N] alias"
    )


def _next_beta(parsed: Version, major: int, minor: int, patch: int) -> str:
    if parsed.pre and parsed.pre[0] == "b":
        return f"{major}.{minor}.{patch}b{parsed.pre[1] + 1}"
    if parsed.pre and parsed.pre[0] == "a":
        return f"{major}.{minor}.{patch}b1"
    return f"{major}.{minor}.{patch + 1}b1"


def calculate_new_version(
    current_version: str, bump_type: int, custom_version: str | None = None
) -> str:
    """Calculate a major, minor, patch, beta, or explicit PEP 440 version."""

    try:
        parsed = Version(current_version)
    except InvalidVersion as exc:
        raise ValueError(f"Invalid current version: {current_version}") from exc

    if bump_type == BumpType.CUSTOM:
        return _validated_custom_version(custom_version)

    major, minor, patch = (*parsed.release, 0, 0)[:3]
    if bump_type == BumpType.MAJOR:
        return f"{major + 1}.0.0"
    if bump_type == BumpType.MINOR:
        return f"{major}.{minor + 1}.0"
    if bump_type == BumpType.PATCH:
        if parsed.is_prerelease:
            return f"{major}.{minor}.{patch}"
        return f"{major}.{minor}.{patch + 1}"
    if bump_type == BumpType.BETA:
        return _next_beta(parsed, major, minor, patch)
    raise ValueError(f"Unknown bump type: {bump_type}")


def _replace_project_version(source: str, new_version: str, path: Path) -> str:
    section_start, section_end = project_section_span(source, path)
    section = source[section_start:section_end]
    updated, replacements = re.subn(
        r'(?m)^(version\s*=\s*)"[^"]+"[ \t]*(?=\r?$)',
        rf'\g<1>"{new_version}"',
        section,
        count=1,
    )
    if replacements != 1:
        raise ValueError(f"[project].version is missing from {path}")
    return source[:section_start] + updated + source[section_end:]


def update_package_version(package_name: str, new_version: str, dry_run: bool = False) -> None:
    """Update one package version while preserving TOML formatting and comments."""

    path = pyproject_path(package_name)
    current_version = get_package_version(package_name)
    if current_version == new_version:
        logging.getLogger("release").debug("%s is already at %s.", package_name, new_version)
        return
    if dry_run:
        logging.getLogger("release").info(
            "[DRY RUN] Would update %s to %s",
            PACKAGES[package_name]["pyproject"],
            new_version,
        )
        return

    source = read_source(path)
    updated = _replace_project_version(source, new_version, path)
    # Parse before writing so malformed output never replaces package metadata.
    tomllib.loads(updated)
    path.write_text(updated, encoding="utf-8", newline="")


def update_all_package_versions(new_versions: dict[str, str], dry_run: bool = False) -> None:
    """Update the configured packages present in ``new_versions``."""

    unknown = set(new_versions).difference(PACKAGES)
    if unknown:
        raise ValueError(f"Unknown release packages: {', '.join(sorted(unknown))}")
    for package_name, new_version in new_versions.items():
        update_package_version(package_name, new_version, dry_run)


_CLASSIFIERS = {
    "alpha": "Development Status :: 3 - Alpha",
    "beta": "Development Status :: 4 - Beta",
    "stable": "Development Status :: 5 - Production/Stable",
}


def get_development_status_classifier(version: str) -> str:
    """Infer a PyPI development-status classifier from a PEP 440 version."""

    try:
        parsed = Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"Invalid version: {version}") from exc
    if parsed.pre:
        return _CLASSIFIERS["alpha" if parsed.pre[0] == "a" else "beta"]
    return _CLASSIFIERS["stable"]


def _existing_development_status(path: Path) -> str | None:
    project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    for classifier in project.get("classifiers", []):
        if isinstance(classifier, str) and classifier.startswith("Development Status ::"):
            return classifier
    return None


def _target_classifier(package_name: str, version: str, development_status: str | None) -> str:
    if development_status is not None:
        try:
            return _CLASSIFIERS[development_status]
        except KeyError as exc:
            raise ValueError("development_status must be alpha, beta, or stable") from exc

    inferred = get_development_status_classifier(version)
    if Version(version).is_prerelease:
        return inferred

    # A stable-looking semantic version does not automatically mean the project
    # has left Alpha/Beta. Preserve the repository's explicit maturity policy.
    current = _existing_development_status(pyproject_path(package_name))
    if current:
        return current
    workspace = _existing_development_status(pyproject_path(WORKSPACE_PACKAGE))
    return workspace or inferred


def update_development_status_classifier(
    package_name: str,
    version: str,
    dry_run: bool = False,
    development_status: str | None = None,
) -> None:
    """Update an existing package classifier without reformatting TOML.

    Packages that omit classifiers remain unchanged unless the caller provides
    an explicit status. For final versions, the existing workspace maturity is
    preserved unless ``development_status`` is supplied.
    """

    path = pyproject_path(package_name)
    source = read_source(path)
    current = _existing_development_status(path)
    if current is None:
        if development_status is not None:
            target = _target_classifier(package_name, version, development_status)
            if dry_run:
                logging.getLogger("release").info(
                    "[DRY RUN] Would add %s classifier: %s",
                    package_name,
                    target,
                )
                return
            _, section_end = project_section_span(source, path)
            insertion = f'classifiers = [\n    "{target}",\n]\n\n'
            updated = source[:section_end].rstrip() + "\n" + insertion + source[section_end:]
            tomllib.loads(updated)
            path.write_text(updated, encoding="utf-8", newline="")
            return
        logging.getLogger("release").debug(
            "%s has no Development Status classifier; leaving it unchanged.", package_name
        )
        return
    target = _target_classifier(package_name, version, development_status)
    if current == target:
        return
    if dry_run:
        logging.getLogger("release").info(
            "[DRY RUN] Would update %s classifier: %s → %s",
            package_name,
            current,
            target,
        )
        return
    classifier_pattern = re.compile(rf"(?P<quote>['\"]){re.escape(current)}(?P=quote)")
    matches = tuple(classifier_pattern.finditer(source))
    if len(matches) != 1:
        raise ValueError(f"Could not locate exactly one classifier {current!r} in {path}")
    updated = classifier_pattern.sub(
        lambda match: f"{match.group('quote')}{target}{match.group('quote')}",
        source,
        count=1,
    )
    tomllib.loads(updated)
    path.write_text(updated, encoding="utf-8", newline="")


def update_all_development_status_classifiers(
    new_versions: dict[str, str],
    dry_run: bool = False,
    development_status: str | None = None,
) -> None:
    """Update development status for each versioned workspace project."""

    for package_name, version in new_versions.items():
        update_development_status_classifier(package_name, version, dry_run, development_status)


def get_internal_package_names() -> set[str]:
    """Return publishable internal distribution names."""

    return set(PACKAGES).difference({WORKSPACE_PACKAGE})


def _update_dependency_string(
    dependency: str,
    internal_names: set[str],
    target_version: str,
    current_project_name: str,
) -> tuple[str, bool]:
    """Pin one internal PEP 508-style dependency while preserving extras/markers."""

    requirement, separator, marker = dependency.partition(";")
    match = re.match(r"^\s*([A-Za-z0-9_.-]+)(\[[^\]]+\])?.*$", requirement)
    if match is None:
        return dependency, False
    name, extras = match.group(1), match.group(2) or ""
    normalized_internal = {item.casefold() for item in internal_names}
    if name.casefold() not in normalized_internal:
        return dependency, False
    if name.casefold() == current_project_name.casefold():
        return dependency, False
    updated = f"{name}{extras}=={target_version}"
    if separator:
        updated = f"{updated}; {marker.strip()}"
    return updated, updated != dependency


def _project_requirements(project: dict[str, object]) -> list[str]:
    dependencies = project.get("dependencies", [])
    requirements = (
        [item for item in dependencies if isinstance(item, str)]
        if isinstance(dependencies, list)
        else []
    )
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for values in optional.values():
            if isinstance(values, list):
                requirements.extend(item for item in values if isinstance(item, str))
    return requirements


def update_internal_dependencies_for_package(
    package_name: str,
    internal_names: set[str],
    target_version: str,
    dry_run: bool = False,
) -> list[tuple[str, str]]:
    """Pin direct and optional internal requirements for one package."""

    path = pyproject_path(package_name)
    source = read_source(path)
    project = tomllib.loads(source).get("project", {})
    current_project_name = str(project.get("name", package_name))
    changes: list[tuple[str, str]] = []
    for dependency in _project_requirements(project):
        updated, changed = _update_dependency_string(
            dependency, internal_names, target_version, current_project_name
        )
        if changed:
            changes.append((dependency, updated))

    if dry_run or not changes:
        return changes

    updated_source = source
    for old, new in changes:
        double_quoted = f'"{old}"'
        single_quoted = f"'{old}'"
        if double_quoted in updated_source:
            updated_source = updated_source.replace(double_quoted, f'"{new}"', 1)
        elif single_quoted in updated_source:
            updated_source = updated_source.replace(single_quoted, f"'{new}'", 1)
        else:
            raise ValueError(f"Could not locate dependency {old!r} in {path}")
    tomllib.loads(updated_source)
    path.write_text(updated_source, encoding="utf-8", newline="")
    return changes


def update_all_internal_dependencies_versions(target_version: str, dry_run: bool = False) -> None:
    """Pin every internal edge to the coordinated release version."""

    internal_names = get_internal_package_names()
    for package_name in PACKAGES:
        if package_name == WORKSPACE_PACKAGE:
            continue
        changes = update_internal_dependencies_for_package(
            package_name, internal_names, target_version, dry_run
        )
        if dry_run:
            for old, new in changes:
                logging.getLogger("release").info("[DRY RUN] %s: %s → %s", package_name, old, new)
