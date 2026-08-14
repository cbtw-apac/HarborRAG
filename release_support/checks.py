"""Local repository checks and GitHub release operations."""

import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from packaging.version import InvalidVersion, Version

from .config import REPOSITORY_ROOT

load_dotenv(REPOSITORY_ROOT / ".env", override=False)

_DRY_RUN_READ_ONLY_COMMANDS = {
    ("git", "branch"),
    ("git", "fetch"),
    ("git", "log"),
    ("git", "ls-remote"),
    ("git", "remote"),
    ("git", "rev-list"),
    ("git", "rev-parse"),
    ("git", "status"),
}


@dataclass(frozen=True)
class CommandResult:
    """Captured result for one release subprocess."""

    stdout: str
    stderr: str
    returncode: int


def _command_result(cmd: str, dry_run: bool = False) -> CommandResult:
    args = shlex.split(cmd)
    if not args:
        raise ValueError("Release command cannot be empty")

    command_family = tuple(args[:2])
    if dry_run and command_family not in _DRY_RUN_READ_ONLY_COMMANDS:
        logging.getLogger("release").debug("[DRY RUN] Would execute: %s", cmd)
        return CommandResult("", "", 0)

    logging.getLogger("release").debug("Executing: %s", shlex.join(args))
    completed = subprocess.run(
        args,
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    return CommandResult(completed.stdout.strip(), completed.stderr.strip(), completed.returncode)


def run_command(cmd: str, dry_run: bool = False) -> tuple[str, str]:
    """Run a release command without invoking a shell.

    The tuple return shape is retained for compatibility with the former
    monolithic release script. Callers that perform safety checks use the
    private result object so a failed command cannot look like an empty success.
    """

    result = _command_result(cmd, dry_run)
    if result.returncode:
        logger = logging.getLogger("release")
        logger.error("Command failed (%s): %s", result.returncode, cmd)
        if result.stderr:
            logger.error("%s", result.stderr)
    return result.stdout, result.stderr


def require_command(cmd: str, dry_run: bool = False) -> str:
    """Run a command and stop the release immediately when it fails."""

    result = _command_result(cmd, dry_run)
    if result.returncode:
        logger = logging.getLogger("release")
        logger.error("Command failed (%s): %s", result.returncode, cmd)
        if result.stderr:
            logger.error("%s", result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout


def _check_result(result: CommandResult, failure_message: str, dry_run: bool) -> bool:
    if result.returncode == 0:
        return True
    logging.getLogger("release").error("%s", failure_message)
    if result.stderr:
        logging.getLogger("release").debug("%s", result.stderr)
    if not dry_run:
        raise SystemExit(1)
    return False


def check_git_status(dry_run: bool = False) -> bool:
    """Require a clean worktree before a real release."""

    result = _command_result("git status --porcelain", dry_run)
    if not _check_result(result, "Unable to inspect the Git worktree.", dry_run):
        return False
    if not result.stdout:
        return True
    logging.getLogger("release").error(
        "There are uncommitted changes. Commit or stash them before releasing."
    )
    if not dry_run:
        raise SystemExit(1)
    return False


def check_current_branch(dry_run: bool = False) -> bool:
    """Require the local ``main`` branch."""

    result = _command_result("git branch --show-current", dry_run)
    if not _check_result(result, "Unable to determine the current branch.", dry_run):
        return False
    if result.stdout == "main":
        return True
    logging.getLogger("release").error(
        "Releases must run from main (current branch: %s).", result.stdout or "detached HEAD"
    )
    if not dry_run:
        raise SystemExit(1)
    return False


def check_unpushed_commits(dry_run: bool = False) -> bool:
    """Require every local main commit to exist on ``origin/main``."""

    result = _command_result("git rev-list --count origin/main..HEAD", dry_run)
    if not _check_result(result, "Unable to compare HEAD with origin/main.", dry_run):
        return False
    try:
        unpushed = int(result.stdout)
    except ValueError:
        logging.getLogger("release").error("Git returned an invalid commit count.")
        if not dry_run:
            raise SystemExit(1)
        return False
    if unpushed == 0:
        return True
    logging.getLogger("release").error(
        "%d commit(s) have not been pushed to origin/main.", unpushed
    )
    if not dry_run:
        raise SystemExit(1)
    return False


def check_main_up_to_date(dry_run: bool = False) -> bool:
    """Require HEAD and the freshly fetched ``origin/main`` to be identical."""

    fetch = _command_result("git fetch --tags origin main", dry_run)
    if not _check_result(fetch, "Unable to fetch origin/main.", dry_run):
        return False
    comparison = _command_result("git rev-list --left-right --count HEAD...origin/main", dry_run)
    if not _check_result(comparison, "Unable to compare HEAD with origin/main.", dry_run):
        return False
    if comparison.stdout.split() == ["0", "0"]:
        return True
    logging.getLogger("release").error(
        "Local main and origin/main differ (%s). Pull with --ff-only first.",
        comparison.stdout or "unknown comparison",
    )
    if not dry_run:
        raise SystemExit(1)
    return False


def check_release_tags_absent(
    package_names: list[str], version: str, dry_run: bool = False
) -> bool:
    """Prevent an existing package tag from being moved or overwritten."""

    existing: list[str] = []
    for package_name in package_names:
        tag = f"{package_name}-v{version}"
        result = _command_result(f"git tag --list {tag}", dry_run)
        if not _check_result(result, f"Unable to inspect release tag {tag}.", dry_run):
            return False
        if result.stdout:
            existing.append(tag)
    if not existing:
        return True
    logging.getLogger("release").error(
        "Release tags already exist and will not be moved: %s", ", ".join(existing)
    )
    if not dry_run:
        raise SystemExit(1)
    return False


def get_github_token(dry_run: bool = False) -> str:
    """Return the GitHub token required for workflow and release API calls."""

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        return token
    logging.getLogger("release").error(
        "GITHUB_TOKEN is required for workflow checks and GitHub releases."
    )
    if not dry_run:
        raise SystemExit(1)
    return ""


def extract_repo_info(git_url: str, dry_run: bool = False) -> str:
    """Extract ``owner/repository`` from a supported GitHub remote URL."""

    candidate = git_url.strip()
    path = ""
    if candidate.startswith("git@github.com:"):
        path = candidate.removeprefix("git@github.com:")
    else:
        parsed = urlparse(candidate)
        if parsed.hostname == "github.com" and parsed.scheme in {"http", "https", "ssh"}:
            path = parsed.path.lstrip("/")

    parts = path.removesuffix(".git").strip("/").split("/")
    if len(parts) == 2 and all(parts):
        return f"{parts[0]}/{parts[1]}"

    logging.getLogger("release").error(
        "Could not parse a GitHub repository from remote URL: %s", git_url
    )
    if dry_run:
        return "unknown/repo"
    raise SystemExit(1)


def extract_changelog_for_version(version: str) -> str:
    """Return one version section from the root changelog."""

    changelog = REPOSITORY_ROOT / "CHANGELOG.md"
    if not changelog.is_file():
        return ""
    content = changelog.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^## \[{re.escape(version)}\](?:[^\n]*)\n(.*?)(?=^## |\Z)",
        content,
    )
    return match.group(1).strip() if match else ""


def check_changelog_updated(new_version: str, dry_run: bool = False) -> bool:
    """Require the first released changelog section to match ``new_version``."""

    changelog = REPOSITORY_ROOT / "CHANGELOG.md"
    if not changelog.is_file():
        logging.getLogger("release").error("CHANGELOG.md is missing.")
        if not dry_run:
            raise SystemExit(1)
        return False

    headings = re.findall(
        r"(?m)^## \[((?!Unreleased\])[^\]]+)\](?:\s+-\s+[^\n]+)?$",
        changelog.read_text(encoding="utf-8"),
    )
    found = headings[0] if headings else None
    if found == new_version:
        return True
    logging.getLogger("release").error(
        "CHANGELOG.md must place '## [%s] - <date>' immediately after Unreleased; found %s.",
        new_version,
        found or "no released version",
    )
    if not dry_run:
        raise SystemExit(1)
    return False


def create_github_release(
    package_name: str, version: str, token: str, dry_run: bool = False
) -> None:
    """Create one GitHub release whose tag triggers the package publisher."""

    tag_name = f"{package_name}-v{version}"
    logger = logging.getLogger("release")
    if dry_run:
        logger.info("[DRY RUN] Would create GitHub release %s", tag_name)
        return

    remote = _command_result("git remote get-url origin")
    if remote.returncode:
        logger.error("Unable to read the origin remote.")
        raise SystemExit(1)
    repository = extract_repo_info(remote.stdout)
    notes = extract_changelog_for_version(version)
    if not notes:
        logger.error("No changelog notes found for version %s.", version)
        raise SystemExit(1)

    try:
        prerelease = Version(version).is_prerelease
    except InvalidVersion:
        logger.error("Invalid release version: %s", version)
        raise SystemExit(1) from None

    response = requests.post(
        f"https://api.github.com/repos/{repository}/releases",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "tag_name": tag_name,
            "name": f"{package_name} v{version}",
            "body": notes,
            "draft": False,
            "prerelease": prerelease,
        },
        timeout=30,
    )
    if response.status_code != 201:
        logger.error(
            "GitHub release creation failed for %s (HTTP %s): %s",
            package_name,
            response.status_code,
            response.text[:500],
        )
        raise SystemExit(1)
    logger.info("Created GitHub release %s", tag_name)
