from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.unit

PACKAGE = Path(__file__).resolve().parents[1]
_DOTENV_IMPORT = re.compile(r"^\s*(from|import)\s+dotenv\b", re.MULTILINE)


def test_python_dotenv_is_declared_by_runtime_not_by_the_mcp_server() -> None:
    dependencies = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["dependencies"]
    sources = (PACKAGE / "src").rglob("*.py")

    assert not any(_DOTENV_IMPORT.search(path.read_text(encoding="utf-8")) for path in sources)
    assert "python-dotenv" not in {Requirement(requirement).name for requirement in dependencies}
