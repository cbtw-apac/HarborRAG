"""The shared operations layer and the CLI must not depend on the HTTP package."""

import ast
from pathlib import Path

import pytest

import harborrag_app

pytestmark = pytest.mark.unit

APP_ROOT = Path(harborrag_app.__file__).parent
API = "harborrag_app.api"
CLI = "harborrag_app.cli"


def _module_name(path: Path) -> str:
    parts = path.relative_to(APP_ROOT.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imported_modules(path: Path) -> set[str]:
    """Absolute dotted names a module imports, relative imports resolved, at any depth."""
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = ".".join(package.split(".")[: len(package.split(".")) - node.level + 1])
                base = f"{anchor}.{base}" if base else anchor
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _importers_of(folder: str, forbidden: tuple[str, ...]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted((APP_ROOT / folder).rglob("*.py")):
        hits = sorted(
            name
            for name in _imported_modules(path)
            if any(name == root or name.startswith(f"{root}.") for root in forbidden)
        )
        if hits:
            found[_module_name(path)] = hits
    return found


def test_operations_imports_neither_api_nor_cli() -> None:
    assert _importers_of("operations", (API, CLI)) == {}


def test_cli_imports_no_api_module() -> None:
    assert _importers_of("cli", (API,)) == {}
