"""Regression: path and glob settings keep the spaces at their ends."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
from harborrag_runtime.config.connectors.models.github import GitHubConfig
from harborrag_runtime.config.connectors.models.local_file import LocalFileSyncConfig
from harborrag_runtime.config.connectors.models.sharepoint import SharePointConfig
from harborrag_runtime.config.connectors.providers import coerce_config_values

pytestmark = pytest.mark.unit

SPACED_PATHS = ["Archive ", " Lead Folder", "Q3\u00a0Plans\u00a0"]


def test_local_path_settings_are_kept_as_typed() -> None:
    """A folder named with a leading or trailing space can be targeted and excluded."""
    config = LocalFileSyncConfig(
        source_path="/data/Shared Docs ",
        include_paths=SPACED_PATHS,
        exclude_paths=SPACED_PATHS,
        include_globs=["Drafts /*.md"],
        exclude_globs=["Drafts /*.tmp"],
        excluded_dir_names=["build "],
    )

    assert config.source_path == "/data/Shared Docs "
    assert config.include_paths == SPACED_PATHS
    assert config.exclude_paths == SPACED_PATHS
    assert config.include_globs == ["Drafts /*.md"]
    assert config.exclude_globs == ["Drafts /*.tmp"]
    assert config.excluded_dir_names == ["build "]


def test_github_path_settings_are_kept_as_typed() -> None:
    """GitHub folders and globs keep their edge spaces, while other values are trimmed."""
    config = GitHubConfig(
        owner=" acme ",
        repo="handbook",
        root_path="Design Docs ",
        include_paths=SPACED_PATHS,
        exclude_paths=SPACED_PATHS,
        include_globs=["Drafts /**"],
        exclude_globs=["Drafts /*.tmp"],
    )

    assert config.owner == "acme"
    assert config.root_path == "Design Docs "
    assert config.include_paths == SPACED_PATHS
    assert config.exclude_paths == SPACED_PATHS
    assert config.include_globs == ["Drafts /**"]
    assert config.exclude_globs == ["Drafts /*.tmp"]


def test_sharepoint_root_path_is_kept_as_typed() -> None:
    """A SharePoint folder root keeps a trailing no-break space."""
    config = SharePointConfig(
        site_id="contoso.sharepoint.com,SITE,WEB", root_path="Q3\u00a0Plans\u00a0"
    )

    assert config.root_path == "Q3\u00a0Plans\u00a0"


@pytest.mark.parametrize("blank", ["", "   ", "\u00a0"])
def test_local_blank_source_path_is_still_rejected(blank: str) -> None:
    """A source path made only of whitespace is rejected, not kept as a folder name."""
    with pytest.raises(ValidationError):
        LocalFileSyncConfig(source_path=blank)


def test_imported_list_keeps_spaces_while_comma_text_is_split_and_trimmed() -> None:
    """A YAML list keeps each path as typed; one comma-separated string is split and trimmed."""
    values = coerce_config_values(
        GitHubRepositoryConfig,
        {"exclude_paths": ["Archive ", " Lead Folder"], "include_paths": "docs, guides"},
    )

    config = GitHubConfig(owner="acme", repo="handbook", **values)

    assert config.exclude_paths == ["Archive ", " Lead Folder"]
    assert config.include_paths == ["docs", "guides"]
