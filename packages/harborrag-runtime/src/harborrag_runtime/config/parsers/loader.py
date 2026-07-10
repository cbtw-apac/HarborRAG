from __future__ import annotations

from pathlib import Path

from harborrag_runtime.config.errors import ParserConfigurationError
from harborrag_runtime.config.parsers.models import ParserCatalog
from harborrag_runtime.config.parsers.schema import parse_parser_definitions
from harborrag_runtime.config.utils import (
    read_yaml_file,
    reject_unknown_keys,
    require_schema_version,
    require_string_mapping,
)

PARSER_CONFIG_VERSION = 1

_ROOT_KEYS = frozenset({"parsers", "version"})


def load_parser_catalog(path: str | Path) -> ParserCatalog:
    """Load and structurally validate a versioned parser YAML catalog."""
    source_path, raw = read_yaml_file(
        path,
        label="Parser configuration",
        error_type=ParserConfigurationError,
    )
    root = require_string_mapping(
        raw,
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
    return ParserCatalog(
        parse_parser_definitions(raw_parsers),
        source_path=source_path,
        version=version,
    )
