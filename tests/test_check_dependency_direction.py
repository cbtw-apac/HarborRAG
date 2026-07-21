"""Tests for scripts/check_dependency_direction.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_dependency_direction import (
    ALLOWED_IMPORTS,
    MODULE_TO_PACKAGE_DIR,
    find_violations,
)

pytestmark = [pytest.mark.unit]


def test_real_repository_has_no_dependency_direction_violations() -> None:
    assert find_violations() == []


def _write_package(
    tmp_path: Path, package_dir: str, module: str, *, src_files: dict[str, str]
) -> None:
    src_dir = tmp_path / "packages" / package_dir / "src" / module
    src_dir.mkdir(parents=True, exist_ok=True)
    for name, content in src_files.items():
        (src_dir / name).write_text(content, encoding="utf-8")


def test_detects_comma_separated_import_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    # harborrag_core must never import anything -- a comma-form import must
    # still be caught even though it isn't the first name after "import".
    _write_package(
        tmp_path,
        "harborrag-core",
        "harborrag_core",
        src_files={"bad.py": "import sys, harborrag_adapters\n"},
    )

    violations = find_violations()

    assert any("harborrag_core must not import harborrag_adapters" in v for v in violations)


def test_detects_dynamic_import_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    _write_package(
        tmp_path,
        "harborrag-core",
        "harborrag_core",
        src_files={
            "bad.py": (
                "import importlib\n"
                "importlib.import_module('harborrag_runtime.foo')\n"
            )
        },
    )

    violations = find_violations()

    assert any("harborrag_core must not import harborrag_runtime" in v for v in violations)


def test_detects_violation_in_packages_own_tests_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    tests_dir = tmp_path / "packages" / "harborrag-core" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_bad.py").write_text("import harborrag_adapters\n", encoding="utf-8")

    violations = find_violations()

    assert any(
        "packages/harborrag-core/tests/test_bad.py" in v and "must not import harborrag_adapters" in v
        for v in violations
    )


def test_allowed_imports_do_not_trigger_violations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    _write_package(
        tmp_path,
        "harborrag-adapters",
        "harborrag_adapters",
        src_files={"ok.py": "from harborrag_core.domain import thing\n"},
    )

    violations = find_violations()

    assert violations == []


def test_allowed_imports_table_matches_module_to_package_dir_keys() -> None:
    assert set(ALLOWED_IMPORTS) == set(MODULE_TO_PACKAGE_DIR)
