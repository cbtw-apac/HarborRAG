"""Source-type schemas: one pydantic model per auth method and one config model per type."""

from .builder import ConnectorConfigBuilder, to_connector_config
from .registry import SourceTypeRegistry, source_types, validate
from .spec import (
    METHOD_KEY,
    AuthMethodSpec,
    Capabilities,
    ConnectionValidationError,
    Deployment,
    FieldIssue,
    SourceTypeSpec,
    ValidatedConnection,
)

__all__ = [
    "METHOD_KEY",
    "AuthMethodSpec",
    "Capabilities",
    "ConnectionValidationError",
    "ConnectorConfigBuilder",
    "Deployment",
    "FieldIssue",
    "SourceTypeRegistry",
    "SourceTypeSpec",
    "ValidatedConnection",
    "source_types",
    "to_connector_config",
    "validate",
]
