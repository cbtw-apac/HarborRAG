"""Operator-facing coordinated release command."""

import logging

import click
from packaging.version import Version

from .checks import (
    check_changelog_updated,
    check_current_branch,
    check_git_status,
    check_main_up_to_date,
    check_release_tags_absent,
    check_unpushed_commits,
    create_github_release,
    get_github_token,
    require_command,
)
from .config import PACKAGES, PRIMARY_PACKAGE
from .metadata import update_release_metadata
from .versioning import (
    calculate_new_version,
    update_all_development_status_classifiers,
    update_all_internal_dependencies_versions,
)
from .versions import (
    assert_release_files_exist,
    get_all_package_versions,
    get_current_version,
    get_package_version,
    get_packages_for_release,
    setup_logging,
    sync_all_package_versions,
    update_all_package_versions,
)
from .workflows import check_github_workflows

_BUMP_TYPES = {"major": 1, "minor": 2, "patch": 3, "beta": 4}


def _run_initial_checks(dry_run: bool) -> dict[str, bool]:
    return {
        "clean worktree": check_git_status(dry_run),
        "main branch": check_current_branch(dry_run),
        "main synchronized": check_main_up_to_date(dry_run),
        "no unpushed commits": check_unpushed_commits(dry_run),
        "critical workflows": check_github_workflows(dry_run),
    }


def _show_checks(checks: dict[str, bool]) -> None:
    click.echo("\nRelease gates")
    click.echo("─" * 42)
    for name, passed in checks.items():
        click.echo(f"{'✅' if passed else '❌'} {name}")


def _select_version(current_version: str, bump: str | None, version: str | None) -> str:
    if version is not None:
        return calculate_new_version(current_version, 5, version)
    if bump is not None:
        return calculate_new_version(current_version, _BUMP_TYPES[bump])

    choices = {
        index: (name, calculate_new_version(current_version, bump_type))
        for index, (name, bump_type) in enumerate(_BUMP_TYPES.items(), start=1)
    }
    click.echo("\nVersion bump")
    click.echo("─" * 42)
    for index, (name, candidate) in choices.items():
        click.echo(f"{index}. {name.title():<6} {current_version} → {candidate}")
    click.echo("5. Custom")
    selected = click.prompt("Select version bump type", type=click.IntRange(1, 5))
    if selected == 5:
        explicit = click.prompt("New version", type=str).strip()
        return calculate_new_version(current_version, 5, explicit)
    return choices[selected][1]


def _update_workspace(
    version: str,
    *,
    dry_run: bool,
    development_status: str | None,
) -> None:
    versions = dict.fromkeys(PACKAGES, version)
    update_all_package_versions(versions, dry_run)
    update_all_development_status_classifiers(versions, dry_run, development_status)
    update_all_internal_dependencies_versions(version, dry_run)
    update_release_metadata(version, dry_run)


def _tag_release(version: str, dry_run: bool) -> None:
    for package_name in get_packages_for_release():
        tag = f"{package_name}-v{version}"
        require_command(f'git tag -a {tag} -m "Release {package_name} v{version}"', dry_run)
    require_command("git push origin --tags", dry_run)


def _synchronize_versions(*, dry_run: bool, development_status: str | None) -> None:
    source_version = get_package_version(PRIMARY_PACKAGE)
    click.echo(f"Synchronizing workspace projects to {source_version}")
    sync_all_package_versions(source_version, dry_run)
    versions = dict.fromkeys(PACKAGES, source_version)
    update_all_development_status_classifiers(versions, dry_run, development_status)
    update_all_internal_dependencies_versions(source_version, dry_run)
    update_release_metadata(source_version, dry_run)
    if not dry_run:
        click.echo("Workspace metadata synchronized; review and commit the changes.")


def _show_dry_run_plan(
    *,
    checks: dict[str, bool],
    changelog_ready: bool,
    version: str,
    development_status: str | None,
) -> None:
    _show_checks({**checks, "changelog updated": changelog_ready})
    click.echo("\nFuture package tags after the prepared release is merged:")
    for package_name in get_packages_for_release():
        click.echo(f"  • {package_name}-v{version}")
    _update_workspace(
        version,
        dry_run=True,
        development_status=development_status,
    )


def _prepare_release(
    *,
    dry_run: bool,
    bump: str | None,
    version: str | None,
    development_status: str | None,
) -> None:
    get_all_package_versions()
    current_version = get_current_version()
    new_version = _select_version(current_version, bump, version)
    if Version(new_version) <= Version(current_version):
        raise click.UsageError("Prepared version must be newer than the current version.")

    click.echo(f"\nPrepare coordinated release: {current_version} → {new_version}")
    if not click.confirm(f"Prepare version {new_version}?", default=True):
        raise click.Abort()
    changelog_ready = check_changelog_updated(new_version, dry_run)
    if dry_run:
        _show_dry_run_plan(
            checks={},
            changelog_ready=changelog_ready,
            version=new_version,
            development_status=development_status,
        )
        return
    if not changelog_ready:
        raise SystemExit(1)

    _update_workspace(
        new_version,
        dry_run=False,
        development_status=development_status,
    )
    click.echo(
        "\nRelease metadata prepared. Review the diff, run the full gates, and "
        "merge it through a pull request before publishing."
    )


def _publish_release(*, dry_run: bool) -> None:
    checks = _run_initial_checks(dry_run)
    get_all_package_versions()
    current_version = get_current_version()
    checks["changelog updated"] = check_changelog_updated(current_version, dry_run)
    packages = get_packages_for_release()
    checks["release tags absent"] = check_release_tags_absent(packages, current_version, dry_run)
    if dry_run:
        _show_checks(checks)
        click.echo("\nPackage tags to publish:")
        for package_name in packages:
            click.echo(f"  • {package_name}-v{current_version}")
        return
    if not all(checks.values()):
        logging.getLogger("release").error("One or more release gates failed.")
        raise SystemExit(1)
    if not click.confirm(f"Publish HarborRAG {current_version}?", default=False):
        raise click.Abort()

    _tag_release(current_version, dry_run=False)
    token = get_github_token()
    for package_name in packages:
        create_github_release(package_name, current_version, token)
    click.echo(f"\nPublished HarborRAG {current_version} across all public packages.")


@click.command()
@click.option(
    "--dry-run", is_flag=True, help="Show checks and changes without mutating the repository."
)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.option(
    "--sync-versions",
    is_flag=True,
    help=f"Synchronize all projects to the {PRIMARY_PACKAGE} version and exit.",
)
@click.option(
    "--publish",
    is_flag=True,
    help="Publish the already-reviewed version on main without changing files.",
)
@click.option(
    "--bump",
    type=click.Choice(tuple(_BUMP_TYPES), case_sensitive=False),
    help="Select a non-interactive version bump.",
)
@click.option("--version", help="Use an explicit non-interactive release version.")
@click.option(
    "--development-status",
    type=click.Choice(("alpha", "beta", "stable"), case_sensitive=False),
    help="Explicitly change PyPI maturity; otherwise preserve it for final versions.",
)
def release(  # noqa: PLR0913 - Click exposes each option as a callback argument.
    dry_run: bool = False,
    verbose: bool = False,
    sync_versions: bool = False,
    publish: bool = False,
    bump: str | None = None,
    version: str | None = None,
    development_status: str | None = None,
) -> None:
    """Prepare and publish one coordinated HarborRAG workspace release."""

    setup_logging(verbose)
    assert_release_files_exist()
    if bump and version:
        raise click.UsageError("Use either --bump or --version, not both.")
    if publish and (sync_versions or bump or version or development_status):
        raise click.UsageError(
            "--publish uses the reviewed current version and cannot be combined "
            "with preparation or synchronization options."
        )

    if sync_versions:
        _synchronize_versions(dry_run=dry_run, development_status=development_status)
        return

    if publish:
        _publish_release(dry_run=dry_run)
        return

    if dry_run:
        click.echo("DRY RUN — no repository or GitHub changes will be made.")
    _prepare_release(
        dry_run=dry_run,
        bump=bump,
        version=version,
        development_status=development_status,
    )
