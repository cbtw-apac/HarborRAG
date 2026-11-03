"""The built API cannot drift from ``contracts/http-v2.0.0.json`` (Story 2 AC18)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from harborrag_app.api.contract import CONTRACT_PATH, drift, load_contract, operations
from harborrag_app.api.export_openapi import export_openapi

_ERROR_REF = {"$ref": "#/components/schemas/Error"}
_STANDARD_ERRORS = {"401", "403", "429", "500", "503", "504"}


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    return json.loads(export_openapi())


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return load_contract()


def test_the_contract_file_sits_at_the_repository_root() -> None:
    repository = Path(__file__).resolve().parents[3]

    assert CONTRACT_PATH == repository / "contracts" / "http-v2.0.0.json"
    assert CONTRACT_PATH.is_file()


def test_the_api_matches_the_contract(openapi: dict[str, Any], contract: dict[str, Any]) -> None:
    problems = drift(contract, operations(openapi))

    assert problems == [], (
        "The API drifted from contracts/http-v2.0.0.json; if the change is intended, review it "
        "and run python -m harborrag_app.api.contract --write\n" + "\n".join(problems)
    )


def test_a_removed_route_fails(openapi: dict[str, Any], contract: dict[str, Any]) -> None:
    mutated = copy.deepcopy(openapi)
    del mutated["paths"]["/v1/me"]

    assert drift(contract, operations(mutated)) == ["GET /v1/me: in the contract but not served"]


def test_an_undeclared_route_fails(openapi: dict[str, Any], contract: dict[str, Any]) -> None:
    mutated = copy.deepcopy(openapi)
    mutated["paths"]["/v1/undeclared"] = {"get": {"responses": {"200": {"description": "OK"}}}}

    assert drift(contract, operations(mutated)) == [
        "GET /v1/undeclared: served but not in the contract"
    ]


def test_a_renamed_required_response_field_fails(
    openapi: dict[str, Any], contract: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(openapi)
    me = mutated["components"]["schemas"]["MeOut"]
    me["required"] = ["tenant" if name == "tenant_id" else name for name in me["required"]]
    me["properties"]["tenant"] = me["properties"].pop("tenant_id")

    problems = drift(contract, operations(mutated))

    assert 'GET /v1/me $.responses["200"]: lost required field tenant_id' in problems
    assert 'GET /v1/me $.responses["200"]: gained required field tenant' in problems


def test_a_lowered_role_fails(openapi: dict[str, Any], contract: dict[str, Any]) -> None:
    mutated = copy.deepcopy(openapi)
    mutated["paths"]["/v1/admin/projects"]["get"]["x-min-role"] = "editor"

    assert drift(contract, operations(mutated)) == [
        "GET /v1/admin/projects $.min_role: the contract says admin, the API says editor"
    ]


def test_every_operation_declares_its_role(openapi: dict[str, Any]) -> None:
    for path, item in openapi["paths"].items():
        if path.startswith("/v1/"):
            for method, operation in item.items():
                assert operation.get("x-min-role") in {"reader", "editor", "admin", "owner"}, (
                    method,
                    path,
                )


def test_every_error_response_uses_the_shared_error_schema(openapi: dict[str, Any]) -> None:
    for path, item in openapi["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for method, operation in item.items():
            responses = operation["responses"]
            assert _STANDARD_ERRORS <= set(responses), (method, path)
            for status, response in responses.items():
                if int(status) >= 400:
                    schema = response["content"]["application/json"]["schema"]
                    assert schema == _ERROR_REF, (method, path, status)


def test_framework_validation_schemas_are_gone(openapi: dict[str, Any]) -> None:
    schemas = openapi["components"]["schemas"]

    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas
    assert set(schemas["Error"]["required"]) == {"error"}
