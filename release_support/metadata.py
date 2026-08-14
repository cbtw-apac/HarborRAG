"""Non-Python release metadata synchronized with workspace versions."""

import json
import logging

from .checks import require_command
from .config import REPOSITORY_ROOT

TYPESCRIPT_PACKAGE = REPOSITORY_ROOT / "clients/typescript/package.json"


def update_typescript_version(version: str, dry_run: bool = False) -> None:
    """Synchronize the generated TypeScript client's package version."""

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
