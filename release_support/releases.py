"""Idempotent GitHub release creation for coordinated package tags."""

import logging
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

from .checks import _command_result, extract_changelog_for_version, extract_repo_info
from .diagnostics import redact_diagnostic
from .github_api import GitHubRequestError, GitHubResponse, github_request

_CREATE_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class ReleaseSpec:
    """Expected identity and metadata for one package release."""

    package_name: str
    version: str
    prerelease: bool

    @property
    def tag_name(self) -> str:
        """Return the coordinated package tag."""

        return f"{self.package_name}-v{self.version}"

    @property
    def release_name(self) -> str:
        """Return the public GitHub release name."""

        return f"{self.package_name} v{self.version}"


def _release_matches(response: GitHubResponse, spec: ReleaseSpec) -> bool:
    """Return whether an existing release matches the requested package release."""

    payload = response.payload
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("tag_name") == spec.tag_name
        and payload.get("name") == spec.release_name
        and payload.get("draft") is False
        and payload.get("prerelease") is spec.prerelease
    )


def _lookup_release(repository: str, token: str, spec: ReleaseSpec) -> bool:
    """Find and validate a tag-specific release, returning false for HTTP 404."""

    response = github_request(repository, f"releases/tags/{spec.tag_name}", token)
    if response.status_code == 404:
        return False
    if response.status_code != 200:
        logging.getLogger("release").error(
            "GitHub release lookup failed for %s (HTTP %s): %s",
            spec.tag_name,
            response.status_code,
            redact_diagnostic(response.text[:500]),
        )
        raise SystemExit(1)
    if not _release_matches(response, spec):
        logging.getLogger("release").error(
            "Existing GitHub release metadata does not match %s.", spec.tag_name
        )
        raise SystemExit(1)
    return True


def _already_exists(response: GitHubResponse) -> bool:
    """Detect GitHub's structured duplicate-release response."""

    payload = response.payload
    errors = payload.get("errors", []) if isinstance(payload, dict) else []
    return any(
        isinstance(error, dict) and error.get("code") == "already_exists" for error in errors
    )


def _release_payload(spec: ReleaseSpec, notes: str) -> dict[str, object]:
    """Build the canonical GitHub release payload for one package."""

    return {
        "tag_name": spec.tag_name,
        "name": spec.release_name,
        "body": notes,
        "draft": False,
        "prerelease": spec.prerelease,
    }


def _lookup_or_fail(repository: str, token: str, spec: ReleaseSpec) -> bool:
    """Convert a transport failure during release lookup into a safe release stop."""

    try:
        return _lookup_release(repository, token, spec)
    except GitHubRequestError as exc:
        logging.getLogger("release").error(
            "Unable to verify GitHub release %s: %s",
            spec.tag_name,
            exc,
        )
        raise SystemExit(1) from exc


def _create_release(
    repository: str,
    token: str,
    spec: ReleaseSpec,
    payload: dict[str, object],
) -> None:
    """Create a release with one safe retry after ambiguous transport failure."""

    logger = logging.getLogger("release")
    for attempt in range(_CREATE_ATTEMPTS):
        try:
            response = github_request(repository, "releases", token, payload=payload)
        except GitHubRequestError as exc:
            logger.error("GitHub release request failed for %s: %s", spec.tag_name, exc)
            if _lookup_or_fail(repository, token, spec):
                logger.info("GitHub release %s exists after an interrupted request.", spec.tag_name)
                return
            if attempt + 1 < _CREATE_ATTEMPTS:
                continue
            raise SystemExit(1) from exc

        if response.status_code == 201:
            logger.info("Created GitHub release %s", spec.tag_name)
            return
        if response.status_code == 422 and _already_exists(response):
            if _lookup_or_fail(repository, token, spec):
                logger.info(
                    "GitHub release %s already exists with matching metadata.", spec.tag_name
                )
                return
        logger.error(
            "GitHub release creation failed for %s (HTTP %s): %s",
            spec.package_name,
            response.status_code,
            redact_diagnostic(response.text[:500]),
        )
        raise SystemExit(1)
    raise SystemExit(1)


def create_github_release(
    package_name: str, version: str, token: str, dry_run: bool = False
) -> None:
    """Create or validate one GitHub release whose tag triggers publication."""

    logger = logging.getLogger("release")
    if dry_run:
        logger.info("[DRY RUN] Would create GitHub release %s-v%s", package_name, version)
        return
    try:
        spec = ReleaseSpec(package_name, version, Version(version).is_prerelease)
    except InvalidVersion:
        logger.error("Invalid release version: %s", version)
        raise SystemExit(1) from None

    remote = _command_result("git remote get-url origin")
    if remote.returncode:
        logger.error("Unable to read the origin remote.")
        raise SystemExit(1)
    repository = extract_repo_info(remote.stdout)
    notes = extract_changelog_for_version(version)
    if not notes:
        logger.error("No changelog notes found for version %s.", version)
        raise SystemExit(1)
    if _lookup_or_fail(repository, token, spec):
        logger.info("GitHub release %s already exists with matching metadata.", spec.tag_name)
        return
    _create_release(repository, token, spec, _release_payload(spec, notes))
