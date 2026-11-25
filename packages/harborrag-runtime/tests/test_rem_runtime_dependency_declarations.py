from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.unit

PACKAGE = Path(__file__).resolve().parents[1]


def _declared_names() -> set[str]:
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = [
        *project["dependencies"],
        *(item for extra in project["optional-dependencies"].values() for item in extra),
    ]
    return {Requirement(requirement).name for requirement in requirements}


def _imported(module: str) -> bool:
    pattern = re.compile(rf"^\s*(from|import)\s+{module}\b", re.MULTILINE)
    return any(
        pattern.search(path.read_text(encoding="utf-8")) for path in (PACKAGE / "src").rglob("*.py")
    )


@pytest.mark.parametrize("name", ["jsonschema", "cloudpickle"])
def test_runtime_declares_no_library_its_source_never_imports(name: str) -> None:
    assert not _imported(name)
    assert name not in _declared_names()
