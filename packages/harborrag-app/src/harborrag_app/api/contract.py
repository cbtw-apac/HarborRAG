"""The v2.0.0 HTTP contract and the drift check against it.

Handles:
- Reducing an OpenAPI document to one entry per operation: lowest role, parameters, the
  request body's required fields, and each response's required fields or shared ``Error``
- Naming every difference from ``contracts/http-v2.0.0.json`` by operation and JSON path
- ``python -m harborrag_app.api.contract --write`` to regenerate the file in a checkout,
  after the change it records has been reviewed

Does NOT judge whether a change is allowed; the CI test fails on any drift and the reviewed
regeneration is the approval.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

_SOURCE = Path(__file__).resolve()
# The file lives in a checkout: packages/harborrag-app/src/harborrag_app/api/contract.py.
_REPOSITORY = _SOURCE.parents[5] if len(_SOURCE.parents) > 5 else Path.cwd()
CONTRACT_PATH = _REPOSITORY / "contracts" / "http-v2.0.0.json"
_PROBES = frozenset({"/healthz", "/readyz"})
_LEGACY_PREFIXES = ("/v1/ingestions",)
_METHODS = ("get", "put", "post", "patch", "delete")
_ERROR_REF = "#/components/schemas/Error"
_REF_PREFIX = "#/components/schemas/"


def _component(ref: str, components: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resolve a local component reference."""
    name = ref.removeprefix(_REF_PREFIX)
    return name, components.get(name, {})


def _join(base: str, name: str) -> str:
    """Append a field name to a dotted path."""
    return f"{base}.{name}" if base else name


def _required(
    schema: Mapping[str, Any],
    components: Mapping[str, Any],
    base: str = "",
    seen: frozenset[str] = frozenset(),
) -> Iterator[str]:
    """Yield the dotted path of every required field reachable from ``schema``."""
    if "$ref" in schema:
        name, schema = _component(schema["$ref"], components)
        if name in seen:
            return
        seen = seen | {name}
    for key in ("allOf", "anyOf", "oneOf"):
        for option in schema.get(key, []):
            yield from _required(option, components, base, seen)
    if "items" in schema:
        yield from _required(schema["items"], components, f"{base}[]", seen)
    properties: Mapping[str, Any] = schema.get("properties", {})
    for name in schema.get("required", []):
        path = _join(base, name)
        yield path
        if name in properties:
            yield from _required(properties[name], components, path, seen)


def _shape(media: Mapping[str, Any] | None, components: Mapping[str, Any]) -> Any:
    """The contract entry of one request or response body."""
    if not media:
        return None
    if "application/json" not in media:
        return sorted(media)
    schema = media["application/json"].get("schema", {})
    if schema.get("$ref") == _ERROR_REF:
        return "Error"
    return sorted(set(_required(schema, components)))


def _parameters(operation: Mapping[str, Any]) -> list[str]:
    """Each parameter as ``in:name``, with ``?`` when optional."""
    return sorted(
        f"{item['in']}:{item['name']}{'' if item.get('required') else '?'}"
        for item in operation.get("parameters", [])
    )


def _covered(path: str) -> bool:
    """Whether the contract covers ``path``."""
    return path.startswith("/v1/") or path in _PROBES


def operations(openapi: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Reduce an OpenAPI document to the contract's entry per operation.

    Args:
        openapi: The document the API publishes at ``/openapi.json``.

    Returns:
        Entries keyed by ``METHOD /path``.
    """
    components = openapi.get("components", {}).get("schemas", {})
    reduced: dict[str, dict[str, Any]] = {}
    for path, item in openapi.get("paths", {}).items():
        if not _covered(path):
            continue
        for method in _METHODS:
            operation = item.get(method)
            if operation is None:
                continue
            body = operation.get("requestBody", {}).get("content")
            reduced[f"{method.upper()} {path}"] = {
                "min_role": operation.get("x-min-role"),
                "legacy": path.startswith(_LEGACY_PREFIXES),
                "parameters": _parameters(operation),
                "request": _shape(body, components),
                "responses": {
                    status: _shape(response.get("content"), components)
                    for status, response in sorted(operation.get("responses", {}).items())
                },
            }
    return reduced


def _list_drift(location: str, expected: list[Any], actual: list[Any]) -> Iterator[str]:
    """Name each field one list has that the other lacks."""
    for missing in sorted(set(expected) - set(actual)):
        yield f"{location}: lost required field {missing}"
    for added in sorted(set(actual) - set(expected)):
        yield f"{location}: gained required field {added}"


def _drift(location: str, expected: Any, actual: Any) -> Iterator[str]:
    """Name each difference between two contract values under ``location``."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(expected.keys() | actual.keys()):
            child = f"{location}.{key}" if key.isidentifier() else f'{location}["{key}"]'
            if key not in actual:
                yield f"{child}: in the contract but not served"
            elif key not in expected:
                yield f"{child}: served but not in the contract"
            else:
                yield from _drift(child, expected[key], actual[key])
    elif isinstance(expected, list) and isinstance(actual, list):
        yield from _list_drift(location, expected, actual)
    elif expected != actual:
        yield f"{location}: the contract says {expected}, the API says {actual}"


def drift(contract: Mapping[str, Any], served: Mapping[str, dict[str, Any]]) -> list[str]:
    """List every difference between the contract and the served operations.

    Args:
        contract: The loaded ``contracts/http-v2.0.0.json``.
        served: ``operations()`` of the API's OpenAPI document.

    Returns:
        One message per difference, each naming the operation and the JSON path.
    """
    expected: Mapping[str, Any] = contract["operations"]
    problems = [
        f"{key}: in the contract but not served" for key in sorted(expected.keys() - served.keys())
    ]
    problems += [
        f"{key}: served but not in the contract" for key in sorted(served.keys() - expected.keys())
    ]
    for key in sorted(expected.keys() & served.keys()):
        problems += [f"{key} {message}" for message in _drift("$", expected[key], served[key])]
    return problems


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    """Read the contract file."""
    return dict(json.loads(path.read_text(encoding="utf-8")))


def render_contract(openapi: Mapping[str, Any]) -> str:
    """Render the contract file for ``openapi`` as stable, reviewable JSON."""
    document = {
        "title": "HarborRAG HTTP API",
        "version": "2.0.0",
        "operations": operations(openapi),
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Check the API against the contract, or rewrite the contract with ``--write``."""
    from .export_openapi import export_openapi

    parser = argparse.ArgumentParser(prog="python -m harborrag_app.api.contract")
    parser.add_argument("--write", action="store_true", help="rewrite the contract file")
    args = parser.parse_args(argv)
    openapi = json.loads(export_openapi())
    if args.write:
        CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT_PATH.write_text(render_contract(openapi), encoding="utf-8")
        sys.stdout.write(f"wrote {CONTRACT_PATH}\n")
        return 0
    problems = drift(load_contract(), operations(openapi))
    for problem in problems:
        sys.stdout.write(problem + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CONTRACT_PATH", "drift", "load_contract", "operations", "render_contract"]
