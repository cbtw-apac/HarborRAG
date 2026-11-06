"""Definitions of the connectors HarborRAG ships.

The registry loads these on its first lookup, so importing one connector
module never imports the others. A plugin registers its own definition the
same way, through ``connector_registry.register_provider``.

Start from: builtin_connector_definitions.
"""

from __future__ import annotations

from .registry import ConnectorProviderDefinition

__all__ = ["builtin_connector_definitions"]

_RATE_LIMITED = {"rate_limiter": "rate_limiter"}


def builtin_connector_definitions() -> tuple[ConnectorProviderDefinition, ...]:
    """Return the definitions of the five built-in connectors.

    Importing the provider packages here, not at module level, keeps them out
    of every process that never looks a connector up.

    Returns:
        Confluence, GitHub, Jira, local file and SharePoint definitions.
    """
    from .confluence import ConfluenceConnector, ConfluenceDocumentTransform, ConfluenceSpaceConfig
    from .github import GitHubConnector, GitHubRepositoryConfig
    from .jira import JiraConnector, JiraDocumentTransform, JiraProjectConfig
    from .local import LocalDocumentTransform, LocalFileConfig, LocalFileConnector
    from .sharepoint import SharePointConnector, SharePointSiteConfig

    return (
        ConnectorProviderDefinition(
            "confluence",
            ConfluenceConnector,
            aliases=("confluence_cloud", "confluence_datacenter"),
            config_factory=ConfluenceSpaceConfig,
            constructor_dependencies=_RATE_LIMITED,
            document_kind="page",
            document_transform_factory=ConfluenceDocumentTransform,
        ),
        ConnectorProviderDefinition(
            "github",
            GitHubConnector,
            aliases=("github_repo", "github_repository"),
            config_factory=GitHubRepositoryConfig,
            constructor_dependencies=_RATE_LIMITED,
            document_kind="file",
        ),
        ConnectorProviderDefinition(
            "jira",
            JiraConnector,
            aliases=("jira_cloud", "jira_datacenter"),
            config_factory=JiraProjectConfig,
            constructor_dependencies=_RATE_LIMITED,
            document_kind="issue",
            document_transform_factory=JiraDocumentTransform,
        ),
        ConnectorProviderDefinition(
            "local",
            LocalFileConnector,
            aliases=("filesystem", "files", "local_files"),
            config_factory=LocalFileConfig,
            config_path_fields=("source_path",),
            document_kind="file",
            document_transform_factory=LocalDocumentTransform,
        ),
        ConnectorProviderDefinition(
            "sharepoint",
            SharePointConnector,
            aliases=("microsoft_sharepoint", "sharepoint_online"),
            config_factory=SharePointSiteConfig,
            constructor_dependencies=_RATE_LIMITED,
            document_kind="file",
        ),
    )
