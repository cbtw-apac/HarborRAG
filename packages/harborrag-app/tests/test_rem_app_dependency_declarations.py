from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.unit

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _project() -> dict[str, Any]:
    return dict(tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"])


def _names(requirements: list[str]) -> set[str]:
    return {Requirement(requirement).name for requirement in requirements}


def test_the_api_extra_declares_the_redis_client_its_limiters_import() -> None:
    assert "redis" in _names(_project()["optional-dependencies"]["api"])


def test_the_base_declares_sqlalchemy_which_the_auth_keys_command_imports() -> None:
    assert "sqlalchemy" in _names(_project()["dependencies"])
