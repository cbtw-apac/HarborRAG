class ConfigurationError(ValueError):
    """Base error for invalid or unreadable HarborRAG configuration."""


class ConnectorConfigurationError(ConfigurationError):
    """Raised when a connector catalog or connector definition is invalid."""


class ConnectorDisabledError(ConnectorConfigurationError):
    """The named connector is configured with ``enabled: false``.

    Tenant-facing callers report it exactly like an unknown connector, so a
    disabled connection is not distinguishable from one that does not exist.
    """


class ConnectorNotFoundError(ConnectorConfigurationError):
    """No connector by that name is available to the requesting tenant.

    Raised with one message for a name that is not configured, a name whose
    ``tenants`` allowlist excludes the caller, and a definition that failed
    to parse, so it is never an oracle for another tenant's connections.
    """


class ParserConfigurationError(ConfigurationError):
    """Raised when a parser catalog or parser definition is invalid."""


class TemporalConfigurationError(ConfigurationError):
    """Raised when Temporal runtime configuration is invalid or unreadable."""


class ScheduleConfigurationError(ConfigurationError):
    """Raised when declarative schedule configuration is invalid or unreadable."""


class GraphBuildConfigurationError(ConfigurationError):
    """Raised when graph-build policy is invalid or unreadable."""
