"""The wheel fallback stays aligned with the workspace MCP configuration."""

from __future__ import annotations

from pathlib import Path

import yaml
from catalog_support import EXPECTED_READER_TOOLS

from harborrag_mcp_server.__main__ import _PACKAGED_CONFIG_PATH

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_packaged_configuration_matches_workspace_defaults() -> None:
    packaged = yaml.safe_load(_PACKAGED_CONFIG_PATH.read_text(encoding="utf-8"))
    workspace = yaml.safe_load((_REPOSITORY_ROOT / "config/mcp.yaml").read_text(encoding="utf-8"))

    assert packaged == workspace


def test_packaged_configuration_lists_every_catalog_tool() -> None:
    # An unlisted tool is still enabled with only its built-in ceilings, so a
    # tool added to the catalog must also appear here for operators to see it.
    packaged = yaml.safe_load(_PACKAGED_CONFIG_PATH.read_text(encoding="utf-8"))

    assert list(packaged["tools"]) == EXPECTED_READER_TOOLS
