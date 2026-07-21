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
import tokenize
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

def _importlib_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Track names bound to the ``importlib`` module and to ``import_module``.

    Handles aliasing (``import importlib as il``, ``from importlib import
    import_module as load``) so a call site can be matched against the real
    binding rather than by name alone.
    """
    module_aliases = set()
    function_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "importlib" and node.level == 0:
                for alias in node.names:
                    if alias.name == "import_module":
                        function_aliases.add(alias.asname or alias.name)
    return module_aliases, function_aliases


def _module_name_argument(call: ast.Call) -> str | None:
    """Extract a literal module name from a call's first arg or ``name=`` kwarg."""
    if call.args:
        first_arg = call.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            return first_arg.value
        return None
    for keyword in call.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                return keyword.value.value
    return None


def _iter_imported_modules(tree: ast.AST) -> Iterator[tuple[str, int]]:
    """Yield every top-level module name a file imports, statically or dynamically."""
    module_aliases, function_aliases = _importlib_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                yield node.module.split(".")[0], node.lineno
        elif isinstance(node, ast.Call):
            callee = node.func
            is_real_dynamic_import = (
                isinstance(callee, ast.Name)
                and (callee.id == "__import__" or callee.id in function_aliases)
            ) or (
                isinstance(callee, ast.Attribute)
                and callee.attr == "import_module"
                and isinstance(callee.value, ast.Name)
                and callee.value.id in module_aliases
            )
            if not is_real_dynamic_import:
                continue
            module_name = _module_name_argument(node)
            if module_name:
                yield module_name.split(".")[0], node.lineno


def _scan_directory(directory: Path, *, module: str, allowed: set[str]) -> list[str]:
    violations: list[str] = []
    if not directory.is_dir():
        return violations
    for path in sorted(directory.rglob("*.py")):
        try:
            with tokenize.open(path) as handle:
                text = handle.read()
            tree = ast.parse(text, filename=str(path))
        except (SyntaxError, OSError, UnicodeError) as exc:
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
