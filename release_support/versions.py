"""Read and update coordinated workspace package versions."""

import logging
import re
import sys
import tomllib
from pathlib import Path

from .config import PACKAGES, PRIMARY_PACKAGE, repository_path
from .metadata import TYPESCRIPT_PACKAGE


class CleanFormatter(logging.Formatter):
    """Compact formatter for an operator-facing release command."""

    PREFIXES = {
        logging.DEBUG: "🔍 ",
        logging.WARNING: "⚠️  ",
        logging.ERROR: "❌ ",
    }

    def format(self, record: logging.LogRecord) -> str:
        return f"{self.PREFIXES.get(record.levelno, '')}{record.getMessage()}"


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure and return the release logger."""

    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(CleanFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logger = logging.getLogger("release")
    logger.setLevel(level)
    return logger


def _pyproject_path(package_name: str) -> Path:
    try:
        relative_path = PACKAGES[package_name]["pyproject"]
    except KeyError as exc:
        raise ValueError(f"Unknown release package: {package_name}") from exc
    return repository_path(relative_path)


def _read_source(path: Path) -> str:
    """Read metadata without normalizing its existing line endings."""

    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def get_package_version(package_name: str) -> str:
    """Return the PEP 621 version for one configured package."""

    path = _pyproject_path(package_name)
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


def _replace_project_version(source: str, new_version: str, path: Path) -> str:
    project_match = re.search(r"(?m)^\[project\]\s*$", source)
    if project_match is None:
        raise ValueError(f"[project] section is missing from {path}")

    next_section = re.search(r"(?m)^\[", source[project_match.end() :])
    section_end = (
        project_match.end() + next_section.start() if next_section is not None else len(source)
    )
    section = source[project_match.end() : section_end]
    updated, replacements = re.subn(
        r'(?m)^(version\s*=\s*)"[^"]+"[ \t]*(?=\r?$)',
        rf'\g<1>"{new_version}"',
        section,
        count=1,
    )
    if replacements != 1:
        raise ValueError(f"[project].version is missing from {path}")
    return source[: project_match.end()] + updated + source[section_end:]


def update_package_version(package_name: str, new_version: str, dry_run: bool = False) -> None:
    """Update one package version while preserving TOML formatting and comments."""

    path = _pyproject_path(package_name)
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

    source = _read_source(path)
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


def sync_all_package_versions(target_version: str, dry_run: bool = False) -> None:
    """Set every workspace project to one coordinated version."""

    for package_name in PACKAGES:
        update_package_version(package_name, target_version, dry_run)


def get_packages_for_release() -> list[str]:
    """Return publishable packages in dependency order with the facade last."""

    packages = [name for name, package in PACKAGES.items() if package["create_release"]]
    if PRIMARY_PACKAGE in packages:
        packages.remove(PRIMARY_PACKAGE)
        packages.append(PRIMARY_PACKAGE)
    return packages


def assert_release_files_exist() -> None:
    """Fail early when release configuration references stale package paths."""

    missing = [
        str(_pyproject_path(name)) for name in PACKAGES if not _pyproject_path(name).is_file()
    ]
    if not TYPESCRIPT_PACKAGE.is_file():
        missing.append(str(TYPESCRIPT_PACKAGE))
    if missing:
        logging.getLogger("release").error(
            "Release configuration references missing package metadata:\n%s",
            "\n".join(f"  - {path}" for path in missing),
        )
        sys.exit(1)
