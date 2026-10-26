"""Non-Python release metadata synchronized with workspace versions."""

import json
import logging
from pathlib import Path

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


def _write_json(path: Path, payload: object) -> None:
    """Write JSON in npm's own layout so only changed values show in the diff."""

    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _update_package_json(version: str, dry_run: bool) -> None:
    payload = json.loads(TYPESCRIPT_PACKAGE.read_text(encoding="utf-8"))
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
    _write_json(TYPESCRIPT_PACKAGE, payload)


def _update_package_lock(version: str, dry_run: bool) -> None:
    """Keep the lockfile's own version fields in step with ``package.json``.

    npm records the root project version twice: at the top level and under
    ``packages[""]``. Neither changes the resolved dependency tree.
    """

    lock_path = TYPESCRIPT_PACKAGE.with_name("package-lock.json")
    if not lock_path.is_file():
        return
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    root_package = payload.get("packages", {}).get("")
    targets = [payload] + ([root_package] if isinstance(root_package, dict) else [])
    stale = [target for target in targets if target.get("version") != version]
    if not stale:
        return
    if dry_run:
        logging.getLogger("release").info(
            "[DRY RUN] Would update clients/typescript/package-lock.json: %s → %s",
            stale[0].get("version"),
            version,
        )
        return
    for target in stale:
        target["version"] = version
    _write_json(lock_path, payload)


def update_typescript_version(version: str, dry_run: bool = False) -> None:
    """Synchronize the generated TypeScript client's package and lock versions."""

    semver = python_version_to_semver(version)
    _update_package_json(semver, dry_run)
    _update_package_lock(semver, dry_run)


def refresh_lockfile(dry_run: bool = False) -> None:
    """Regenerate the uv lock after coordinated metadata changes."""

    require_command("uv lock", dry_run)


def update_release_metadata(version: str, dry_run: bool = False) -> None:
    """Update release-owned metadata outside Python package pyprojects."""

    update_typescript_version(version, dry_run)
    refresh_lockfile(dry_run)
