#!/usr/bin/env python3
"""Backward-compatible entry point for HarborRAG's release command."""

from release_support.checks import (
    check_changelog_updated as check_changelog_updated,
)
from release_support.checks import (
    check_current_branch as check_current_branch,
)
from release_support.checks import (
    check_git_status as check_git_status,
)
from release_support.checks import (
    check_main_up_to_date as check_main_up_to_date,
)
from release_support.checks import (
    check_unpushed_commits as check_unpushed_commits,
)
from release_support.checks import (
    create_github_release as create_github_release,
)
from release_support.checks import (
    extract_changelog_for_version as extract_changelog_for_version,
)
from release_support.checks import (
    extract_repo_info as extract_repo_info,
)
from release_support.checks import (
    get_github_token as get_github_token,
)
from release_support.checks import (
    run_command as run_command,
)
from release_support.cli import release as release
from release_support.config import PACKAGES as PACKAGES
from release_support.versioning import (
    calculate_new_version as calculate_new_version,
)
from release_support.versioning import (
    get_development_status_classifier as get_development_status_classifier,
)
from release_support.versioning import (
    get_internal_package_names as get_internal_package_names,
)
from release_support.versioning import (
    update_all_development_status_classifiers as update_all_development_status_classifiers,
)
from release_support.versioning import (
    update_all_internal_dependencies_versions as update_all_internal_dependencies_versions,
)
from release_support.versioning import (
    update_development_status_classifier as update_development_status_classifier,
)
from release_support.versioning import (
    update_internal_dependencies_for_package as update_internal_dependencies_for_package,
)
from release_support.versions import (
    get_all_package_versions as get_all_package_versions,
)
from release_support.versions import (
    get_current_version as get_current_version,
)
from release_support.versions import (
    get_package_version as get_package_version,
)
from release_support.versions import (
    get_packages_for_release as get_packages_for_release,
)
from release_support.versions import (
    setup_logging as setup_logging,
)
from release_support.versions import (
    sync_all_package_versions as sync_all_package_versions,
)
from release_support.versions import (
    update_all_package_versions as update_all_package_versions,
)
from release_support.versions import (
    update_package_version as update_package_version,
)
from release_support.workflows import (
    check_github_workflows as check_github_workflows,
)

if __name__ == "__main__":
    release()
