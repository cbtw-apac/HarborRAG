from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.unit

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_the_base_declares_python_dotenv_which_the_sharepoint_login_imports() -> None:
    dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]

    assert "python-dotenv" in {Requirement(requirement).name for requirement in dependencies}
