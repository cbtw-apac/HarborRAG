from __future__ import annotations

from collections.abc import Callable, Mapping
from inspect import signature

from harborrag_adapters.connectors import (
    ConfluenceSpaceConfig,
    GitHubRepositoryConfig,
    JiraProjectConfig,
    LocalFileConfig,
    SharePointSiteConfig,
)

type ConnectorConfigFactory = Callable[..., object]

_CONFIG_FACTORIES: Mapping[str, ConnectorConfigFactory] = {
    "confluence": ConfluenceSpaceConfig,
    "github": GitHubRepositoryConfig,
    "jira": JiraProjectConfig,
    "local": LocalFileConfig,
    "sharepoint": SharePointSiteConfig,
}
_PROVIDER_ALIASES: Mapping[str, str] = {
    "confluence_cloud": "confluence",
    "confluence_datacenter": "confluence",
    "github_repo": "github",
    "github_repository": "github",
    "jira_cloud": "jira",
    "jira_datacenter": "jira",
    "filesystem": "local",
    "files": "local",
    "local_files": "local",
    "microsoft_sharepoint": "sharepoint",
    "sharepoint_online": "sharepoint",
}

SECRET_CONFIG_FIELDS = frozenset({"access_token", "client_secret", "token"})
PYTHON_ONLY_CONFIG_FIELDS = frozenset(
    {"custom_parsers", "process_attachment_callback", "process_file_callback"}
)


def canonical_provider_name(provider: str) -> str:
    """Normalize a provider name or alias to its canonical name."""
    normalized = provider.strip()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def config_factory(provider: str) -> ConnectorConfigFactory | None:
    """Return the provider config constructor, or ``None`` if unsupported."""
    return _CONFIG_FACTORIES.get(provider)


def config_field_names(factory: ConnectorConfigFactory) -> set[str]:
    """Return keyword fields accepted by a provider config dataclass."""
    return set(signature(factory).parameters)


def supported_provider_names() -> list[str]:
    """Return canonical provider names in deterministic display order."""
    return sorted(_CONFIG_FACTORIES)
