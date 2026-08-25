"""Non-Python release metadata synchronized with workspace versions."""

import json
import logging

from packaging.version import InvalidVersion, Version

from .checks import require_command
from .config import REPOSITORY_ROOT

TYPESCRIPT_PACKAGE = REPOSITORY_ROOT / "clients/typescript/package.json"


def python_version_to_semver(version: str) -> str:
    """Translate a supported PEP 440 release into npm-compatible SemVer."""

    try:
        parsed = Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"Invalid Python release version: {version}") from exc
    if parsed.dev is not None or parsed.post is not None or parsed.local is not None:
        raise ValueError(f"Unsupported Python release version for npm: {version}")

    major, minor, patch = (*parsed.release, 0, 0)[:3]
    semver = f"{major}.{minor}.{patch}"
    if parsed.pre is None:
        return semver
    phase, number = parsed.pre
    label = {"a": "alpha", "b": "beta", "rc": "rc"}[phase]
    return f"{semver}-{label}.{number}"


def update_typescript_version(version: str, dry_run: bool = False) -> None:
    """Synchronize the generated TypeScript client's package version."""

    payload = json.loads(TYPESCRIPT_PACKAGE.read_text(encoding="utf-8"))
    version = python_version_to_semver(version)
    current = payload.get("version")
    if current == version:
        return
    if dry_run:
        logging.getLogger("release").info(
            "[DRY RUN] Would update clients/typescript/package.json: %s → %s",
            current,
            version,
        )
        return
    payload["version"] = version
    TYPESCRIPT_PACKAGE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def refresh_lockfile(dry_run: bool = False) -> None:
    """Regenerate the uv lock after coordinated metadata changes."""

    require_command("uv lock", dry_run)


def update_release_metadata(version: str, dry_run: bool = False) -> None:
    """Update release-owned metadata outside Python package pyprojects."""

    update_typescript_version(version, dry_run)
    refresh_lockfile(dry_run)
