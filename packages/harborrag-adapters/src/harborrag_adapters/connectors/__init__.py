"""Connectors that pull documents from source systems, and their registry.

Importing this package is cheap: the registry, query and metadata types load
eagerly, while each provider (Confluence, GitHub, Jira, local files,
SharePoint) loads on first access to its names or on the registry's first
lookup. Connectors import ``_shared`` and core, never ``parsers``.

Start from: connector_registry, HarborConnector.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from harborrag_adapters.connectors.descriptors import ConnectorDocumentDescriptor
from harborrag_adapters.connectors.harbor_connector import HarborConnector
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

if TYPE_CHECKING:
    from harborrag_adapters.connectors.confluence import (
        ConfluenceConnector,
        ConfluenceDeploymentType,
        ConfluenceDocumentTransform,
        ConfluenceSpaceConfig,
    )
    from harborrag_adapters.connectors.github import GitHubConnector, GitHubRepositoryConfig
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
    from harborrag_adapters.connectors.sharepoint import SharePointConnector, SharePointSiteConfig

# Provider names this package re-exports, by the subpackage that defines them.
_PROVIDER_EXPORTS = {
    "ConfluenceConnector": "confluence",
    "ConfluenceDeploymentType": "confluence",
    "ConfluenceDocumentTransform": "confluence",
    "ConfluenceSpaceConfig": "confluence",
    "GitHubConnector": "github",
    "GitHubRepositoryConfig": "github",
    "JiraConnector": "jira",
    "JiraDeploymentType": "jira",
    "JiraDocumentTransform": "jira",
    "JiraProjectConfig": "jira",
    "LocalDocumentTransform": "local",
    "LocalFileConfig": "local",
    "LocalFileConnector": "local",
    "SharePointConnector": "sharepoint",
    "SharePointSiteConfig": "sharepoint",
}


def __getattr__(name: str) -> object:
    """Import a provider's public name on first access."""
    subpackage = _PROVIDER_EXPORTS.get(name)
    if subpackage is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{subpackage}"), name)
    globals()[name] = value
    return value


__all__ = [
    "ConfluenceConnector",
    "ConfluenceDeploymentType",
    "ConfluenceDocumentTransform",
    "ConfluenceSpaceConfig",
    "ConnectorCapabilities",
    "ConnectorDocumentDescriptor",
    "ConnectorMetadata",
    "ConnectorProviderDefinition",
    "ConnectorQuery",
    "ConnectorSkip",
    "GitHubConnector",
    "GitHubRepositoryConfig",
    "HarborConnector",
    "JiraConnector",
    "JiraDeploymentType",
    "JiraDocumentTransform",
    "JiraProjectConfig",
    "LocalDocumentTransform",
    "LocalFileConfig",
    "LocalFileConnector",
    "SharePointConnector",
    "SharePointSiteConfig",
    "connector_registry",
]
