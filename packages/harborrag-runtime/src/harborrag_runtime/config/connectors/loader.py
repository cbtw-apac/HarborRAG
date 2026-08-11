from __future__ import annotations

import logging
from pathlib import Path

from harborrag_runtime.config.connectors.parsing import parse_connector_definitions
from harborrag_runtime.config.connectors.schemas import ConnectorCatalog
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.loading import (
    read_yaml_file,
    reject_unknown_keys,
    require_schema_version,
    require_string_mapping,
)

CONNECTOR_CONFIG_VERSION = 1

logger = logging.getLogger("harborrag.runtime.config.connectors")

_ROOT_KEYS = frozenset({"connectors", "version"})


def load_connector_catalog(path: str | Path) -> ConnectorCatalog:
    """Load a connector YAML file into an immutable, validated catalog.

    File loading validates structural concerns without resolving secrets or
    constructing clients. Provider dataclass validation runs later when a
    definition is built, allowing disabled definitions to omit live secrets.

    Args:
        path: Connector YAML file. Relative paths resolve from the current
            process directory.

    An invalid individual connector definition does not fail this call; its
    error is attached to the returned catalog under ``ConnectorCatalog.errors``
    and only raised when that connector is looked up or built by name.

    Raises:
        ConnectorConfigurationError: If the file or root-level schema is
            invalid (unreadable file, bad version, unknown root keys).
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
    definitions, errors = parse_connector_definitions(
        raw_connectors,
        base_directory=source_path.parent,
    )
    catalog = ConnectorCatalog(
        definitions,
        source_path=source_path,
        version=version,
        errors=errors,
    )
    logger.info(
        "Connector catalog loaded path=%s version=%d definitions=%d enabled=%d",
        source_path,
        version,
        len(catalog.connectors),
        len(catalog.names(enabled_only=True)),
    )
    if errors:
        logger.warning(
            "Connector catalog path=%s has %d invalid definition(s) that will "
            "only raise when looked up or built: %s",
            source_path,
            len(errors),
            ", ".join(sorted(errors)),
        )
    return catalog
