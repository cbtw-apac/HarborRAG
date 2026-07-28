"""Check that cross-package imports follow the HarborRAG layering rules.

Allowed direction (lower layers never import higher ones):

    harborrag_core      -> (stdlib only)
    harborrag_adapters  -> core
    harborrag_engine    -> core, adapters
    harborrag_runtime   -> core, adapters, engine
    harborrag_app       -> core, engine, runtime
    harborrag_mcp_server -> core, engine, runtime
    harborrag           -> any harborrag package (public facade)

Each package's own ``tests/`` directory is checked against the same rule as
its ``src/`` tree (a package's tests should not reach into a layer its
production code isn't allowed to depend on either). Both plain
``import``/``from`` statements and dynamic imports
(``importlib.import_module(...)``, ``__import__(...)``) with a literal
string argument are detected via the AST rather than a line-anchored regex,
so multi-import statements (``import harborrag_core, harborrag_runtime``),
multi-line imports, and dynamic imports are all caught.

``smoke`` trees anywhere under ``tests/`` are exempt from this rule, whether
they sit at ``tests/smoke/`` or under a domain directory such as
``tests/connectors/smoke/``. Unlike ordinary pytest suites, those scripts are
manual, opt-in integration checks that deliberately wire up the real
application stack (real declarative config via `harborrag_runtime`, real
credentials) exactly like `harborrag_app` would -- see each suite's
`README.md`.

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
    "harborrag_mcp_server": {"harborrag_core", "harborrag_engine", "harborrag_runtime"},
    "harborrag": {
        "harborrag_core",
        "harborrag_adapters",
        "harborrag_engine",
        "harborrag_runtime",
        "harborrag_app",
        "harborrag_mcp_server",
    },
}

MODULE_TO_PACKAGE_DIR = {
    "harborrag_core": "harborrag-core",
    "harborrag_adapters": "harborrag-adapters",
    "harborrag_engine": "harborrag-engine",
    "harborrag_runtime": "harborrag-runtime",
    "harborrag_app": "harborrag-app",
    "harborrag_mcp_server": "harborrag-mcp-server",
    "harborrag": "harborrag",
}

# Manual, opt-in integration scripts (see the README.md beside each smoke
# suite) intentionally wire up the full application stack and are exempt from
# the layering rule that applies to ordinary pytest suites. Suites are grouped
# by domain, so the exempt directory is nested at any depth under tests/.
EXEMPT_TEST_SUBDIRS = frozenset({"smoke"})


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


_FUNCTION_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_NESTED_SCOPE_NODES = (*_FUNCTION_SCOPE_NODES, ast.ClassDef)


def _assigned_names(target: ast.AST) -> Iterator[str]:
    """Yield every name a (possibly destructuring) assignment target binds."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Starred):
        yield from _assigned_names(target.value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _assigned_names(element)


def _function_local_shadow_names(
    scope: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> set[str]:
    """Names a parameter, assignment, for/with/except target, or walrus
    operator binds directly within this function/lambda's own scope.

    Deliberately excludes names bound by ``import``/``from ... import``: an
    import statement establishes the real dynamic-import binding this
    checker looks for, so it must never be treated as shadowing it. Does
    not descend into nested function/lambda/class scopes, which bind their
    own names independently (a parameter or reassignment there shadows only
    within that inner scope, not this one).
    """
    names: set[str] = set()
    args = scope.args
    for arglist in (args.posonlyargs, args.args, args.kwonlyargs):
        names.update(arg.arg for arg in arglist)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)

    def walk(node: ast.AST) -> None:
        if isinstance(node, _NESTED_SCOPE_NODES):
            return  # separate scope; its own bindings do not leak out here
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_assigned_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_assigned_names(node.target))
        elif isinstance(node, ast.AugAssign):
            names.update(_assigned_names(node.target))
        elif isinstance(node, ast.NamedExpr):
            names.update(_assigned_names(node.target))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            names.update(_assigned_names(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    names.update(_assigned_names(item.optional_vars))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        for child in ast.iter_child_nodes(node):
            walk(child)

    if isinstance(scope, ast.Lambda):
        walk(scope.body)
    else:
        for stmt in scope.body:
            walk(stmt)
    return names


def _iter_calls_with_enclosing_scopes(
    tree: ast.AST,
) -> Iterator[tuple[ast.Call, list[set[str]]]]:
    """Yield every Call node paired with the local-shadow-name sets of all
    its enclosing function/lambda scopes (innermost first).

    A parameter or reassignment inside some unrelated function elsewhere in
    the file must not make a call inside *this* function look like a real
    dynamic import (or vice versa): scoping is resolved per call site
    instead of collecting binding names across the whole file flatly.
    """
    scope_stack: list[set[str]] = []

    def visit(node: ast.AST) -> Iterator[tuple[ast.Call, list[set[str]]]]:
        if isinstance(node, ast.Call):
            yield node, list(scope_stack)
        if isinstance(node, _FUNCTION_SCOPE_NODES):
            scope_stack.append(_function_local_shadow_names(node))
            for child in ast.iter_child_nodes(node):
                yield from visit(child)
            scope_stack.pop()
        else:
            for child in ast.iter_child_nodes(node):
                yield from visit(child)

    yield from visit(tree)


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

    for call, enclosing_scopes in _iter_calls_with_enclosing_scopes(tree):
        callee = call.func
        callee_name = (
            callee.id
            if isinstance(callee, ast.Name)
            else (
                callee.value.id
                if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name)
                else None
            )
        )
        if callee_name is not None and any(callee_name in scope for scope in enclosing_scopes):
            # Shadowed by a parameter/reassignment in an enclosing function:
            # this call cannot refer to the real importlib binding, no
            # matter what the file imports at module scope.
            continue
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
        module_name = _module_name_argument(call)
        if module_name:
            yield module_name.split(".")[0], call.lineno


def _scan_directory(
    directory: Path,
    *,
    module: str,
    allowed: set[str],
    exempt_subdirs: frozenset[str] = frozenset(),
) -> list[str]:
    violations: list[str] = []
    if not directory.is_dir():
        return violations
    for path in sorted(directory.rglob("*.py")):
        # Match directory components only, never the file name itself, so a
        # module called smoke.py stays subject to the rule.
        if exempt_subdirs and exempt_subdirs.intersection(path.relative_to(directory).parts[:-1]):
            continue
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
        violations.extend(
            _scan_directory(
                package_root / "tests",
                module=module,
                allowed=allowed,
                exempt_subdirs=EXEMPT_TEST_SUBDIRS,
            )
        )
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
