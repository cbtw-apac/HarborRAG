"""Check that cross-package imports follow the HarborRAG layering rules.

Allowed direction (lower layers never import higher ones):

    harborrag_core      -> (stdlib only)
    harborrag_adapters  -> core
    harborrag_engine    -> core, adapters
    harborrag_runtime   -> core, adapters, engine
    harborrag_app       -> core, engine, runtime
    harborrag_mcp       -> core, engine, runtime
    harborrag           -> any harborrag package (public facade)

Each package's own ``tests/`` directory is checked against the same rule as
its ``src/`` tree (a package's tests should not reach into a layer its
production code isn't allowed to depend on either). Both plain
``import``/``from`` statements and dynamic imports
(``importlib.import_module(...)``, ``__import__(...)``) with a literal
string argument are detected via the AST rather than a line-anchored regex,
so multi-import statements (``import harborrag_core, harborrag_runtime``),
multi-line imports, and dynamic imports are all caught.

Usage:
    python scripts/check_dependency_direction.py
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWED_IMPORTS: dict[str, set[str]] = {
    "harborrag_core": set(),
    "harborrag_adapters": {"harborrag_core"},
    "harborrag_engine": {"harborrag_core", "harborrag_adapters"},
    "harborrag_runtime": {"harborrag_core", "harborrag_adapters", "harborrag_engine"},
    "harborrag_app": {"harborrag_core", "harborrag_engine", "harborrag_runtime"},
    "harborrag_mcp": {"harborrag_core", "harborrag_engine", "harborrag_runtime"},
    "harborrag": {
        "harborrag_core",
        "harborrag_adapters",
        "harborrag_engine",
        "harborrag_runtime",
        "harborrag_app",
        "harborrag_mcp",
    },
}

MODULE_TO_PACKAGE_DIR = {
    "harborrag_core": "harborrag-core",
    "harborrag_adapters": "harborrag-adapters",
    "harborrag_engine": "harborrag-engine",
    "harborrag_runtime": "harborrag-runtime",
    "harborrag_app": "harborrag-app",
    "harborrag_mcp": "harborrag-mcp",
    "harborrag": "harborrag",
}

_DYNAMIC_IMPORT_CALLEES = {"import_module", "__import__"}


def _iter_imported_modules(tree: ast.AST) -> Iterator[tuple[str, int]]:
    """Yield every top-level module name a file imports, statically or dynamically."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                yield node.module.split(".")[0], node.lineno
        elif isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else (
                callee.id if isinstance(callee, ast.Name) else None
            )
            if name not in _DYNAMIC_IMPORT_CALLEES or not node.args:
                continue
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                yield first_arg.value.split(".")[0], node.lineno


def _scan_directory(directory: Path, *, module: str, allowed: set[str]) -> list[str]:
    violations: list[str] = []
    if not directory.is_dir():
        return violations
    for path in sorted(directory.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            relative = path.relative_to(REPO_ROOT)
            violations.append(f"{relative}: could not parse for import analysis ({exc})")
            continue
        for imported, line in _iter_imported_modules(tree):
            if imported in MODULE_TO_PACKAGE_DIR and imported not in allowed:
                relative = path.relative_to(REPO_ROOT)
                violations.append(f"{relative}:{line}: {module} must not import {imported}")
    return violations


def find_violations() -> list[str]:
    violations: list[str] = []
    for module, package_dir in MODULE_TO_PACKAGE_DIR.items():
        package_root = REPO_ROOT / "packages" / package_dir
        src_dir = package_root / "src" / module
        if not src_dir.is_dir():
            violations.append(f"missing package source directory: {src_dir}")
            continue
        allowed = ALLOWED_IMPORTS[module] | {module}
        violations.extend(_scan_directory(src_dir, module=module, allowed=allowed))
        violations.extend(_scan_directory(package_root / "tests", module=module, allowed=allowed))
    return violations


def main() -> int:
    violations = find_violations()
    if violations:
        print("Dependency direction violations found:")
        for violation in violations:
            print(f"  {violation}")
        return 1
    print("Dependency direction OK: all cross-package imports follow the layering rules.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
