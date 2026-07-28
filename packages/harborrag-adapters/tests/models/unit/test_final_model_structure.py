from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

REPOSITORY = Path(__file__).resolve().parents[5]
SOURCE_PACKAGES = {
    REPOSITORY / "packages/harborrag-adapters/src": "harborrag_adapters.models",
    REPOSITORY / "packages/harborrag-core/src": "harborrag_core.models",
}


def _model_modules() -> dict[str, tuple[Path, ast.Module]]:
    modules: dict[str, tuple[Path, ast.Module]] = {}
    for source, package in SOURCE_PACKAGES.items():
        package_path = source.joinpath(*package.split("."))
        for path in package_path.rglob("*.py"):
            relative = path.relative_to(source).with_suffix("")
            parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
            module = ".".join(parts)
            modules[module] = (path, ast.parse(path.read_text(encoding="utf-8")))
    return modules


def _imports(module: str, tree: ast.Module, *, is_package: bool) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                levels_up = node.level - 1 if is_package else node.level
                parent = module.split(".")[:-levels_up] if levels_up else module.split(".")
                target = ".".join((*parent, *(node.module or "").split("."))).rstrip(".")
            else:
                target = node.module or ""
            if target:
                imported.add(target)
            if node.module is None:
                imported.update(f"{target}.{alias.name}" for alias in node.names)
    return imported


def test_model_classes_are_descriptive_and_cross_package_contracts_stay_clean() -> None:
    modules = _model_modules()
    private_classes: list[str] = []
    forbidden_core_imports: list[str] = []
    provider_packages = {
        "anthropic",
        "boto3",
        "botocore",
        "cohere",
        "google.generativeai",
        "langfuse",
        "litellm",
        "openai",
        "opentelemetry",
    }
    for module, (path, tree) in modules.items():
        private_classes.extend(
            f"{path.name}:{node.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name.startswith("_")
        )
        if module.startswith("harborrag_core.models"):
            forbidden_core_imports.extend(
                name
                for name in _imports(module, tree, is_package=path.name == "__init__.py")
                if any(
                    name == package or name.startswith(f"{package}.")
                    for package in provider_packages
                )
            )
    assert set(private_classes) == {
        "budget.py:_BudgetWindow",
        "routing_state_memory.py:_MemoryState",
    }
    assert forbidden_core_imports == []


def test_model_families_do_not_import_each_other() -> None:
    modules = _model_modules()
    violations: list[tuple[str, str]] = []
    families = ("chat", "embed", "rerank")
    for module, (path, tree) in modules.items():
        family = next((item for item in families if f".models.{item}" in module), None)
        if family is None:
            continue
        for imported in _imports(module, tree, is_package=path.name == "__init__.py"):
            if any(
                f"harborrag_adapters.models.{other}" in imported
                for other in families
                if other != family
            ):
                violations.append((module, imported))
    assert violations == []


def test_model_import_graph_is_acyclic() -> None:
    modules = _model_modules()
    graph = {
        module: {
            target
            for target in _imports(module, tree, is_package=path.name == "__init__.py")
            if target in modules
        }
        for module, (path, tree) in modules.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, trail: tuple[str, ...]) -> None:
        if module in visiting:
            raise AssertionError("circular model import: " + " -> ".join((*trail, module)))
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency, (*trail, module))
        visiting.remove(module)
        visited.add(module)

    for module in graph:
        visit(module, ())
