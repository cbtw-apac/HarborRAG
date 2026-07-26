"""Repo-root discovery and `sys.path` setup shared by every bootstrap submodule.

Connectors and parsers are built from the same declarative sources as the real
application: `config/connectors.yaml` and `config/parsers.yaml` (falling back
to the `.example.yaml` templates when a real file hasn't been created yet).
Environment variables come from `env/.env.connector` and `env/.env.parser`.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[6]

for source_path in (
    REPO_ROOT / "packages" / "harborrag-adapters" / "src",
    REPO_ROOT / "packages" / "harborrag-core" / "src",
    REPO_ROOT / "packages" / "harborrag-runtime" / "src",
):
    source = str(source_path)
    if source not in sys.path:
        sys.path.insert(0, source)

CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
