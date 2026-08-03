"""Protect the internal dependency direction of the chunking bounded context."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import harborrag_engine.ingestion.chunking as chunking

_PACKAGE = "harborrag_engine.ingestion.chunking"
_FORBIDDEN_CONCERNS = {
    "identity": frozenset({"pipeline", "records", "sources", "table", "transforms"}),
    "records": frozenset({"pipeline", "sources", "table", "transforms"}),
    "transforms": frozenset({"pipeline", "records", "sources", "table"}),
    "sources": frozenset({"pipeline", "records", "table"}),
    "table": frozenset({"pipeline", "records", "sources", "transforms"}),
}


def test_lower_chunking_concerns_do_not_import_forbidden_peers() -> None:
    root = Path(chunking.__file__).parent
    violations: list[str] = []

    for concern, forbidden in _FORBIDDEN_CONCERNS.items():
        for source in sorted((root / concern).glob("*.py")):
            module = f"{_PACKAGE}.{concern}.{source.stem}"
            package = module.rpartition(".")[0]
            tree = ast.parse(source.read_text("utf-8"), filename=str(source))
            for node in ast.walk(tree):
                imported = _imported_module(node, package)
                if imported is None or not imported.startswith(f"{_PACKAGE}."):
                    continue
                imported_concern = imported.removeprefix(f"{_PACKAGE}.").split(".", 1)[0]
                if imported_concern in forbidden:
                    violations.append(
                        f"{source.relative_to(root)}:{node.lineno} imports {imported}"
                    )

    assert not violations, "Forbidden chunking dependencies:\n" + "\n".join(violations)


def _imported_module(node: ast.AST, package: str) -> str | None:
    if isinstance(node, ast.ImportFrom):
        if node.level:
            return resolve_name(f"{'.' * node.level}{node.module or ''}", package)
        return node.module
    if isinstance(node, ast.Import) and node.names:
        return node.names[0].name
    return None
