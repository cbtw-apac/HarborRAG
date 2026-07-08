"""Connector provider registry and public connector imports."""

from harborrag_adapters.connectors.confluence import (
    ConfluenceConnector,
    ConfluenceDeploymentType,
    ConfluenceSpaceConfig,
)
from harborrag_adapters.connectors.github import (
    GitHubConnector,
    GitHubRepositoryConfig,
)
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.connectors.jira import (
    JiraConnector,
    JiraDeploymentType,
    JiraProjectConfig,
)
from harborrag_adapters.connectors.local import (
    LocalFileConfig,
    LocalFileConnector,
)
from harborrag_adapters.connectors.registry import connector_registry
from harborrag_adapters.connectors.schemas import (
    ConnectorCapabilities,
    ConnectorQuery,
    ConnectorSyncState,
)
from harborrag_adapters.connectors.sharepoint import (
    SharePointConnector,
    SharePointSiteConfig,
)

connector_registry.register(
    "confluence",
    ConfluenceConnector,
    aliases=["confluence_cloud", "confluence_datacenter"],
)
connector_registry.register(
    "github",
    GitHubConnector,
    aliases=["github_repo", "github_repository"],
)
connector_registry.register(
    "jira",
    JiraConnector,
    aliases=["jira_cloud", "jira_datacenter"],
)
connector_registry.register(
    "local",
    LocalFileConnector,
    aliases=["filesystem", "files", "local_files"],
)
connector_registry.register(
    "sharepoint",
    SharePointConnector,
    aliases=["microsoft_sharepoint", "sharepoint_online"],
)

__all__ = [
    "ConfluenceConnector",
    "ConfluenceDeploymentType",
    "ConfluenceSpaceConfig",
    "ConnectorCapabilities",
    "ConnectorQuery",
    "ConnectorSyncState",
    "GitHubConnector",
    "GitHubRepositoryConfig",
    "HarborConnector",
    "JiraConnector",
    "JiraDeploymentType",
    "JiraProjectConfig",
    "LocalFileConfig",
    "LocalFileConnector",
    "SharePointConnector",
    "SharePointSiteConfig",
    "connector_registry",
]
