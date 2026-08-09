"""Connector provider registry and public connector imports."""

from harborrag_adapters.connectors.confluence import (
    ConfluenceConnector,
    ConfluenceDeploymentType,
    ConfluenceDocumentTransform,
    ConfluenceSpaceConfig,
)
from harborrag_adapters.connectors.descriptors import (
    ConnectorDocumentDescriptor,
)
from harborrag_adapters.connectors.github import (
    GitHubConnector,
    GitHubRepositoryConfig,
)
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.connectors.jira import (
    JiraConnector,
    JiraDeploymentType,
    JiraDocumentTransform,
    JiraProjectConfig,
)
from harborrag_adapters.connectors.local import (
    LocalDocumentTransform,
    LocalFileConfig,
    LocalFileConnector,
)
from harborrag_adapters.connectors.registry import (
    ConnectorProviderDefinition,
    connector_registry,
)
from harborrag_adapters.connectors.schemas import (
    ConnectorCapabilities,
    ConnectorMetadata,
    ConnectorQuery,
    ConnectorSkip,
)
from harborrag_adapters.connectors.sharepoint import (
    SharePointConnector,
    SharePointSiteConfig,
)

connector_registry.register_provider(
    ConnectorProviderDefinition(
        "confluence",
        ConfluenceConnector,
        aliases=("confluence_cloud", "confluence_datacenter"),
        config_factory=ConfluenceSpaceConfig,
        constructor_dependencies={
            "parser": "attachment_parser",
            "rate_limiter": "rate_limiter",
        },
        document_kind="page",
        document_transform_factory=ConfluenceDocumentTransform,
    )
)
connector_registry.register_provider(
    ConnectorProviderDefinition(
        "github",
        GitHubConnector,
        aliases=("github_repo", "github_repository"),
        config_factory=GitHubRepositoryConfig,
        document_kind="file",
    )
)
connector_registry.register_provider(
    ConnectorProviderDefinition(
        "jira",
        JiraConnector,
        aliases=("jira_cloud", "jira_datacenter"),
        config_factory=JiraProjectConfig,
        constructor_dependencies={
            "parser": "attachment_parser",
            "rate_limiter": "rate_limiter",
        },
        document_kind="issue",
        document_transform_factory=JiraDocumentTransform,
    )
)
connector_registry.register_provider(
    ConnectorProviderDefinition(
        "local",
        LocalFileConnector,
        aliases=("filesystem", "files", "local_files"),
        config_factory=LocalFileConfig,
        config_path_fields=("source_path",),
        document_kind="file",
        document_transform_factory=LocalDocumentTransform,
    )
)
connector_registry.register_provider(
    ConnectorProviderDefinition(
        "sharepoint",
        SharePointConnector,
        aliases=("microsoft_sharepoint", "sharepoint_online"),
        config_factory=SharePointSiteConfig,
        document_kind="file",
    )
)
