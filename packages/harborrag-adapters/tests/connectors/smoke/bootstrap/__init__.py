"""Shared setup for standalone connector smoke scripts.

Connectors and parsers are built from the same declarative sources as the real
application: `config/connectors.yaml` and `config/parsers.yaml` (falling back
to the `.example.yaml` templates when a real file hasn't been created yet).
Environment variables come from `env/.env.connector` and `env/.env.parser`.
"""

from __future__ import annotations

from harborrag_runtime.config import ConnectorConfigurationError

from .catalogs import build_connector, connector_catalog, connector_definition, parser_catalog
from .environment import env, load_env
from .metadata_rendering import format_metadata_value, render_metadata_section
from .ocr_parser import RapidOcrImageParser, attachment_custom_parsers, build_harbor_parser
from .output import (
    SUPPORTED_OUTPUT_FORMATS,
    output_path_for,
    sanitize_output_id,
    save_attachment_asset,
    save_output,
)
from .paths import CONFIG_DIR, DEFAULT_OUTPUT_DIR, REPO_ROOT
from .previews import (
    attachments_passed,
    print_attachments,
    print_document,
    print_failure,
    print_parsed,
)

__all__ = [
    "CONFIG_DIR",
    "DEFAULT_OUTPUT_DIR",
    "REPO_ROOT",
    "SUPPORTED_OUTPUT_FORMATS",
    "ConnectorConfigurationError",
    "RapidOcrImageParser",
    "attachment_custom_parsers",
    "attachments_passed",
    "build_connector",
    "build_harbor_parser",
    "connector_catalog",
    "connector_definition",
    "env",
    "format_metadata_value",
    "load_env",
    "output_path_for",
    "parser_catalog",
    "print_attachments",
    "print_document",
    "print_failure",
    "print_parsed",
    "render_metadata_section",
    "sanitize_output_id",
    "save_attachment_asset",
    "save_output",
]
