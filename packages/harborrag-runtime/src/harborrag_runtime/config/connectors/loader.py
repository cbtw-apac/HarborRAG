from __future__ import annotations

from pathlib import Path

from harborrag_runtime.config.connectors.models import ConnectorCatalog
from harborrag_runtime.config.connectors.schema import parse_connector_definitions
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.utils import (
    read_yaml_file,
    reject_unknown_keys,
    require_schema_version,
    require_string_mapping,
)

CONNECTOR_CONFIG_VERSION = 1

_ROOT_KEYS = frozenset({"connectors", "version"})


def load_connector_catalog(path: str | Path) -> ConnectorCatalog:
    """Load a connector YAML file into an immutable, validated catalog.

    File loading validates structural concerns without resolving secrets or
    constructing clients. Provider dataclass validation runs later when a
    definition is built, allowing disabled definitions to omit live secrets.

    Args:
        path: Connector YAML file. Relative paths resolve from the current
            process directory.

    Raises:
        ConnectorConfigurationError: If the file, schema, provider, or field
            definitions are invalid.
    """
    source_path, raw = read_yaml_file(
        path,
        label="Connector configuration",
        error_type=ConnectorConfigurationError,
    )
    root = require_string_mapping(
        raw,
        label="connector configuration root",
        error_type=ConnectorConfigurationError,
    )
    reject_unknown_keys(
        root,
        _ROOT_KEYS,
        label="connector configuration root",
        error_type=ConnectorConfigurationError,
    )

    version = require_schema_version(
        root.get("version"),
        expected=CONNECTOR_CONFIG_VERSION,
        label="Connector configuration",
        error_type=ConnectorConfigurationError,
    )
    raw_connectors = require_string_mapping(
        root.get("connectors"),
        label="connectors",
        error_type=ConnectorConfigurationError,
    )
    definitions = parse_connector_definitions(
        raw_connectors,
        base_directory=source_path.parent,
    )
    return ConnectorCatalog(definitions, source_path=source_path, version=version)
