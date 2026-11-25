from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.unit

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_the_api_extra_declares_the_redis_client_its_limiters_import() -> None:
    api = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["optional-dependencies"][
        "api"
    ]

    assert "redis" in {Requirement(requirement).name for requirement in api}
