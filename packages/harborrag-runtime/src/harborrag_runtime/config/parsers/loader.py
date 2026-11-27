"""Loading of the versioned parser catalog, ``config/ingestion/parsers.yaml``.

Loading validates structure only; logs name the catalog file and counts, never settings.
"""

from __future__ import annotations

import logging
from pathlib import Path

from harborrag_adapters.models.runtime.environment import expand_environment
from harborrag_core.observability.log_events import log_event
from harborrag_runtime.config.errors import ParserConfigurationError
from harborrag_runtime.config.loading import (
    read_yaml_file,
    reject_unknown_keys,
    require_schema_version,
    require_string_mapping,
)
from harborrag_runtime.config.parsers.parsing import parse_parser_definitions
from harborrag_runtime.config.parsers.schemas import ParserCatalog

PARSER_CONFIG_VERSION = 1

logger = logging.getLogger("harborrag.runtime.config.parsers")

_ROOT_KEYS = frozenset({"parsers", "version"})


def load_parser_catalog(path: str | Path) -> ParserCatalog:
    """Load and structurally validate a versioned parser YAML catalog."""
    source_path, raw = read_yaml_file(
        path,
        label="Parser configuration",
        error_type=ParserConfigurationError,
    )
    root = require_string_mapping(
        _expand(raw, source_path),
        label="parser configuration root",
        error_type=ParserConfigurationError,
    )
    reject_unknown_keys(
        root,
        _ROOT_KEYS,
        label="parser configuration root",
        error_type=ParserConfigurationError,
    )

    version = require_schema_version(
        root.get("version"),
        expected=PARSER_CONFIG_VERSION,
        label="Parser configuration",
        error_type=ParserConfigurationError,
    )
    raw_parsers = require_string_mapping(
        root.get("parsers"),
        label="parsers",
        error_type=ParserConfigurationError,
    )
    catalog = ParserCatalog(
        parse_parser_definitions(raw_parsers),
        source_path=source_path,
        version=version,
    )
    log_event(
        logger,
        "config.parser_catalog_loaded",
        config_path=str(source_path),
        schema_version_id=version,
        definitions_count=len(catalog.parsers),
        enabled_count=len(catalog.names(enabled_only=True)),
    )
    return catalog


def _expand(raw: object, source_path: Path) -> object:
    """Resolve ``${VAR}`` and ``${VAR:-default}`` references in the catalog.

    Deployment-specific endpoints (the OCR server URL above all) differ between
    the containerized worker and a host run, so they belong in the environment
    file rather than in an edited copy of the catalog. Credentials keep using
    the ``secrets`` block: settings named after a secret field are rejected
    whatever their value.
    """
    try:
        return expand_environment(raw)
    except ValueError as error:
        raise ParserConfigurationError(
            f"Parser configuration {source_path} references an unset environment "
            f"variable: {error}. Set it, or give the reference a default with "
            f"${{VARIABLE:-value}}."
        ) from error
