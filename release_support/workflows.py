"""GitHub Actions release gate."""

import logging

import requests

from .checks import extract_repo_info, get_github_token, run_command

CRITICAL_WORKFLOWS = {
    "API Contract",
    "Documentation Website (Auto)",
    "Quality Gates",
    "Test and Coverage",
}


def _fail(message: str, dry_run: bool) -> bool:
    logging.getLogger("release").error("%s", message)
    if not dry_run:
        raise SystemExit(1)
    return False


def _actions_runs(
    repository: str,
    token: str,
    *,
    status: str,
    head_sha: str,
) -> list[dict[str, object]] | None:
    params: dict[str, str | int] = {
        "branch": "main",
        "head_sha": head_sha,
        "status": status,
        "per_page": 100,
    }
    try:
        response = requests.get(
            f"https://api.github.com/repos/{repository}/actions/runs",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params=params,
            timeout=30,
        )
    except requests.RequestException as exc:
        logging.getLogger("release").error("GitHub Actions request failed: %s", exc)
        return None
    if response.status_code != 200:
        logging.getLogger("release").error(
            "GitHub Actions returned HTTP %s: %s",
            response.status_code,
            response.text[:500],
        )
        return None
    payload = response.json()
    runs = payload.get("workflow_runs")
    return runs if isinstance(runs, list) else None


def _running_workflow_names(runs: list[dict[str, object]]) -> list[str]:
    return sorted(str(run.get("name", "unknown")) for run in runs)


def _completed_workflow_error(runs: list[dict[str, object]]) -> str | None:
    latest_by_name: dict[str, dict[str, object]] = {}
    for run in runs:
        name = str(run.get("name", ""))
        latest_by_name.setdefault(name, run)

    missing = CRITICAL_WORKFLOWS.difference(latest_by_name)
    if missing:
        return "No completed run exists on the release commit for: " + ", ".join(sorted(missing))

    failed = {
        name: str(latest_by_name[name].get("conclusion", "unknown"))
        for name in CRITICAL_WORKFLOWS
        if latest_by_name[name].get("conclusion") != "success"
    }
    if failed:
        details = ", ".join(f"{name}={status}" for name, status in sorted(failed.items()))
        return f"Critical workflows are not passing: {details}"
    return None


def check_github_workflows(dry_run: bool = False) -> bool:
    """Require all critical workflows to pass on the exact release commit."""

    remote, remote_error = run_command("git remote get-url origin", dry_run)
    if remote_error or not remote:
        return _fail("Unable to read the origin GitHub remote.", dry_run)
    repository = extract_repo_info(remote, dry_run)
    if repository == "unknown/repo":
        return False

    commit, commit_error = run_command("git rev-parse HEAD", dry_run)
    if commit_error or not commit:
        return _fail("Unable to determine the current commit.", dry_run)
    token = get_github_token(dry_run)
    if not token:
        return False

    running = _actions_runs(repository, token, status="in_progress", head_sha=commit)
    if running is None:
        return _fail("Unable to inspect running GitHub workflows.", dry_run)
    if running:
        return _fail(
            f"Workflows are still running for {commit[:12]}: "
            + ", ".join(_running_workflow_names(running)),
            dry_run,
        )

    completed = _actions_runs(repository, token, status="completed", head_sha=commit)
    if completed is None:
        return _fail("Unable to inspect completed GitHub workflows.", dry_run)

    workflow_error = _completed_workflow_error(completed)
    if workflow_error:
        return _fail(workflow_error, dry_run)

    logging.getLogger("release").info("All critical workflows passed on commit %s.", commit[:12])
    return True
