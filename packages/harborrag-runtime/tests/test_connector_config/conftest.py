from __future__ import annotations

from pathlib import Path
from textwrap import dedent

REPO_ROOT = Path(__file__).resolve().parents[4]


def write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config" / "connectors.yaml"
    config_path.parent.mkdir()
    config_path.write_text(dedent(content), encoding="utf-8")
    return config_path
