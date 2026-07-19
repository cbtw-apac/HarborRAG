class ConfigurationError(ValueError):
    """Base error for invalid or unreadable HarborRAG configuration."""


class ConnectorConfigurationError(ConfigurationError):
    """Raised when a connector catalog or connector definition is invalid."""


class ParserConfigurationError(ConfigurationError):
    """Raised when a parser catalog or parser definition is invalid."""
