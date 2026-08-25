"""GitHub Actions release gate."""

import logging

from .checks import extract_repo_info, get_github_token, run_command
from .diagnostics import redact_diagnostic
from .github_api import GitHubRequestError, github_request

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
    head_sha: str,
) -> list[dict[str, object]] | None:
    params: dict[str, str | int] = {
        "branch": "main",
        "head_sha": head_sha,
        "per_page": 100,
    }
    try:
        response = github_request(
            repository,
            "actions/runs",
            token,
            query=params,
        )
    except GitHubRequestError as exc:
        logging.getLogger("release").error(
            "GitHub Actions request failed: %s", redact_diagnostic(exc)
        )
        return None
    if response.status_code != 200:
        logging.getLogger("release").error(
            "GitHub Actions returned HTTP %s: %s",
            response.status_code,
            redact_diagnostic(response.text[:500]),
        )
        return None
    payload = response.payload
    if not isinstance(payload, dict):
        return None
    runs = payload.get("workflow_runs")
    return runs if isinstance(runs, list) else None


def _completed_workflow_error(runs: list[dict[str, object]]) -> str | None:
    latest_by_name: dict[str, dict[str, object]] = {}
    for run in runs:
        name = str(run.get("name", ""))
        latest_by_name.setdefault(name, run)

    missing = CRITICAL_WORKFLOWS.difference(latest_by_name)
    if missing:
        return "No run exists on the release commit for: " + ", ".join(sorted(missing))

    unresolved = {
        name: str(latest_by_name[name].get("status", "unknown"))
        for name in CRITICAL_WORKFLOWS
        if latest_by_name[name].get("status") != "completed"
    }
    if unresolved:
        details = ", ".join(f"{name}={status}" for name, status in sorted(unresolved.items()))
        return f"Critical workflows are not completed: {details}"

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

    completed = _actions_runs(repository, token, head_sha=commit)
    if completed is None:
        return _fail("Unable to inspect GitHub workflows.", dry_run)

    workflow_error = _completed_workflow_error(completed)
    if workflow_error:
        return _fail(workflow_error, dry_run)

    logging.getLogger("release").info("All critical workflows passed on commit %s.", commit[:12])
    return True
