class ConfigurationError(ValueError):
    """Base error for invalid or unreadable HarborRAG configuration."""


class ConnectorConfigurationError(ConfigurationError):
    """Raised when a connector catalog or connector definition is invalid."""


class ParserConfigurationError(ConfigurationError):
    """Raised when a parser catalog or parser definition is invalid."""


class TemporalConfigurationError(ConfigurationError):
    """Raised when Temporal runtime configuration is invalid or unreadable."""


class GraphBuildConfigurationError(ConfigurationError):
    """Raised when graph-build policy is invalid or unreadable."""


class ChunkingConfigurationError(ConfigurationError):
    """Raised when the chunking policy is invalid or unreadable."""
