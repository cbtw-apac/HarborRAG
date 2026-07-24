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


def test_detects_comma_separated_import_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
            "bad.py": ("import importlib\nimportlib.import_module('harborrag_runtime.foo')\n")
        },
    )

    violations = find_violations()

    assert any("harborrag_core must not import harborrag_runtime" in v for v in violations)


def test_parameter_shadowing_import_module_is_not_a_dynamic_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A function parameter named ``import_module`` must shadow the real
    ``importlib.import_module`` binding within that function, even though
    the file also genuinely imports it (and uses it safely) elsewhere."""
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    _write_package(
        tmp_path,
        "harborrag-core",
        "harborrag_core",
        src_files={
            "ok.py": (
                "from importlib import import_module\n"
                "\n"
                "def real_loader(name):\n"
                "    return import_module(name)\n"
                "\n"
                "def unrelated(import_module):\n"
                "    # `import_module` here is just a parameter name, not the\n"
                "    # real function -- this must not be treated as a dynamic\n"
                "    # import even though it names a disallowed module.\n"
                "    return import_module('harborrag_runtime.foo')\n"
            )
        },
    )

    violations = find_violations()

    assert violations == []


def test_reassignment_shadowing_import_module_is_not_a_dynamic_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A local reassignment of ``import_module`` shadows the real binding
    for the rest of that function, per ordinary Python scoping rules."""
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    _write_package(
        tmp_path,
        "harborrag-core",
        "harborrag_core",
        src_files={
            "ok.py": (
                "from importlib import import_module\n"
                "\n"
                "def fake_loader():\n"
                "    import_module = lambda name: None  # noqa: E731\n"
                "    return import_module('harborrag_runtime.foo')\n"
            )
        },
    )

    violations = find_violations()

    assert violations == []


def test_unrelated_import_module_method_call_is_not_a_dynamic_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A method named ``import_module`` on an unrelated object (not the real
    ``importlib.import_module``) must not be treated as a dynamic import."""
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    _write_package(
        tmp_path,
        "harborrag-core",
        "harborrag_core",
        src_files={
            "ok.py": "client.import_module('harborrag_runtime.foo')\n",
        },
    )

    violations = find_violations()

    assert violations == []


def test_detects_aliased_import_module_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``from importlib import import_module as load`` must still be caught."""
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    _write_package(
        tmp_path,
        "harborrag-core",
        "harborrag_core",
        src_files={
            "bad.py": (
                "from importlib import import_module as load\nload('harborrag_runtime.foo')\n"
            )
        },
    )

    violations = find_violations()

    assert any("harborrag_core must not import harborrag_runtime" in v for v in violations)


def test_detects_dynamic_import_via_keyword_name_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``importlib.import_module(name="...")`` must be caught, not just positional args."""
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    _write_package(
        tmp_path,
        "harborrag-core",
        "harborrag_core",
        src_files={
            "bad.py": ("import importlib\nimportlib.import_module(name='harborrag_runtime.foo')\n")
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
        "packages/harborrag-core/tests/test_bad.py" in v
        and "must not import harborrag_adapters" in v
        for v in violations
    )


def test_allowed_imports_do_not_trigger_violations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_smoke_test_directory_is_exempt_from_layering_rule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Manual, opt-in scripts under `tests/smoke/` intentionally wire up the
    full application stack and must not be flagged like ordinary tests."""
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    smoke_dir = tmp_path / "packages" / "harborrag-adapters" / "tests" / "smoke" / "connectors"
    smoke_dir.mkdir(parents=True)
    (smoke_dir / "bootstrap.py").write_text(
        "from harborrag_runtime.config import load_connector_catalog\n",
        encoding="utf-8",
    )

    violations = find_violations()

    assert violations == []


def test_non_smoke_test_directory_is_still_checked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The `tests/smoke/` exemption must not blanket-exempt all of `tests/`."""
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    tests_dir = tmp_path / "packages" / "harborrag-adapters" / "tests" / "unit"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_bad.py").write_text(
        "from harborrag_runtime.config import load_connector_catalog\n",
        encoding="utf-8",
    )

    violations = find_violations()

    assert any("harborrag_adapters must not import harborrag_runtime" in v for v in violations)


def test_non_utf8_declared_encoding_is_honored_not_crashed_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file with a PEP 263 non-UTF-8 encoding declaration must be read with
    that encoding (via tokenize.open) instead of crashing make deps-check."""
    monkeypatch.setattr("scripts.check_dependency_direction.REPO_ROOT", tmp_path)
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        _write_package(tmp_path, package_dir, module, src_files={"__init__.py": ""})
    src_dir = tmp_path / "packages" / "harborrag-core" / "src" / "harborrag_core"
    content = "# -*- coding: latin-1 -*-\n# café\nimport harborrag_adapters\n"
    (src_dir / "bad.py").write_bytes(content.encode("latin-1"))

    violations = find_violations()

    assert any("harborrag_core must not import harborrag_adapters" in v for v in violations)
