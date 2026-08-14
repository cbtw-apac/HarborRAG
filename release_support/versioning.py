"""Version calculation and coordinated package metadata updates."""

import logging
import re
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version

from .config import PACKAGES, WORKSPACE_PACKAGE, repository_path

_CLASSIFIERS = {
    "alpha": "Development Status :: 3 - Alpha",
    "beta": "Development Status :: 4 - Beta",
    "stable": "Development Status :: 5 - Production/Stable",
}
_CUSTOM_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$")


def _validated_custom_version(custom_version: str | None) -> str:
    if custom_version is None or not _CUSTOM_VERSION.fullmatch(custom_version):
        raise ValueError("Custom version must use X.Y.Z, X.Y.ZaN, X.Y.ZbN, or X.Y.ZrcN")
    return custom_version


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

    if bump_type == 5:
        return _validated_custom_version(custom_version)

    major, minor, patch = parsed.release[:3]
    if bump_type == 1:
        return f"{major + 1}.0.0"
    if bump_type == 2:
        return f"{major}.{minor + 1}.0"
    if bump_type == 3:
        if parsed.is_prerelease:
            return f"{major}.{minor}.{patch}"
        return f"{major}.{minor}.{patch + 1}"
    if bump_type == 4:
        return _next_beta(parsed, major, minor, patch)
    raise ValueError(f"Unknown bump type: {bump_type}")


def get_development_status_classifier(version: str) -> str:
    """Infer a PyPI development-status classifier from a PEP 440 version."""

    try:
        parsed = Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"Invalid version: {version}") from exc
    if parsed.pre:
        return _CLASSIFIERS["alpha" if parsed.pre[0] == "a" else "beta"]
    return _CLASSIFIERS["stable"]


def _package_path(package_name: str) -> Path:
    try:
        return repository_path(PACKAGES[package_name]["pyproject"])
    except KeyError as exc:
        raise ValueError(f"Unknown release package: {package_name}") from exc


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
    current = _existing_development_status(_package_path(package_name))
    if current:
        return current
    workspace = _existing_development_status(_package_path(WORKSPACE_PACKAGE))
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

    path = _package_path(package_name)
    source = path.read_text(encoding="utf-8")
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
            project_match = re.search(r"(?m)^\[project\]\s*$", source)
            if project_match is None:
                raise ValueError(f"[project] section is missing from {path}")
            next_section = re.search(r"(?m)^\[", source[project_match.end() :])
            section_end = (
                project_match.end() + next_section.start()
                if next_section is not None
                else len(source)
            )
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
    updated = source.replace(f'"{current}"', f'"{target}"', 1)
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

    path = _package_path(package_name)
    source = path.read_text(encoding="utf-8")
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
