from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.unit

PACKAGE = Path(__file__).resolve().parents[1]


def _project() -> dict[str, Any]:
    document = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(document["project"])


def _names(requirements: list[str]) -> set[str]:
    return {Requirement(requirement).name for requirement in requirements}


def _declared_names() -> set[str]:
    project = _project()
    extras = project["optional-dependencies"].values()
    return _names([*project["dependencies"], *(item for extra in extras for item in extra)])


def _imported(module: str) -> bool:
    pattern = re.compile(rf"^\s*(from|import)\s+{module}\b", re.MULTILINE)
    return any(
        pattern.search(path.read_text(encoding="utf-8")) for path in (PACKAGE / "src").rglob("*.py")
    )


@pytest.mark.parametrize("name", ["jsonschema", "cloudpickle"])
def test_runtime_declares_no_library_its_source_never_imports(name: str) -> None:
    assert not _imported(name)
    assert name not in _declared_names()


def test_the_base_declares_python_dotenv_which_the_checkout_env_reader_imports() -> None:
    assert _imported("dotenv")
    assert "python-dotenv" in _names(_project()["dependencies"])
