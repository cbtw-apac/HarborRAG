"""The OpenAPI document the API publishes at ``/openapi.json``.

Handles:
- ``x-min-role`` on every ``/v1`` operation, read from its route guard
- The error responses every ``/v1`` operation can return, each referencing the shared
  ``Error`` schema, in place of FastAPI's ``HTTPValidationError``

Does NOT change any route; it annotates the document FastAPI generates once per process.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from .auth.access import api_routes, minimum_role, scopes_tenant
from .schemas import Error

_ERROR_REF = {"$ref": "#/components/schemas/Error"}
_STANDARD_ERRORS: dict[int, str] = {
    401: "Missing or invalid credentials (unauthenticated)",
    403: "Role too low (forbidden) or tenant not held (tenant_forbidden)",
    429: "Over the rate limit (rate_limited); see Retry-After",
    500: "Unhandled error (internal_error)",
    503: "A dependency is down (dependency_unavailable)",
    504: "Past the server deadline (deadline_exceeded)",
}
_FRAMEWORK_SCHEMAS = ("HTTPValidationError", "ValidationError")
_PUBLIC_PREFIX = "/v1/"


def _error_response(description: str) -> dict[str, Any]:
    """One documented error response using the shared schema."""
    return {"description": description, "content": {"application/json": {"schema": _ERROR_REF}}}


def _annotate_operation(operation: dict[str, Any], role: str | None, tenant: bool) -> None:
    """Add the role and every error response an operation can return."""
    if role is not None:
        operation["x-min-role"] = role
    responses: dict[str, Any] = operation.setdefault("responses", {})
    errors = dict(_STANDARD_ERRORS)
    if tenant:
        errors[400] = "Several tenants held and no X-Tenant-Id (tenant_required)"
    if "requestBody" in operation:
        errors[413] = "Body over the limit (payload_too_large)"
    if "requestBody" in operation or operation.get("parameters"):
        errors[422] = "A field fails validation (validation_error)"
    for code, description in errors.items():
        responses.setdefault(str(code), _error_response(description))
    for status, response in responses.items():
        if int(status) >= 400:
            response["content"] = {"application/json": {"schema": _ERROR_REF}}


def _annotate(schema: dict[str, Any], app: FastAPI) -> None:
    """Annotate every ``/v1`` operation and keep only the shared error schemas."""
    paths: dict[str, Any] = schema.get("paths", {})
    for route in api_routes(app.routes):
        path = getattr(route, "path_format", None) or route.path
        if not path.startswith(_PUBLIC_PREFIX) or path not in paths:
            continue
        for method in route.methods:
            operation = paths[path].get(method.lower())
            if operation is not None:
                _annotate_operation(operation, minimum_role(route), scopes_tenant(route))
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for name in _FRAMEWORK_SCHEMAS:
        components.pop(name, None)
    if "Error" not in components:
        error = Error.model_json_schema(ref_template="#/components/schemas/{model}")
        components.update(error.pop("$defs", {}))
        components["Error"] = error


def install_openapi(app: FastAPI) -> None:
    """Make ``app.openapi()`` return the annotated document, built once and cached."""
    generate: Callable[[], dict[str, Any]] = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            _annotate(generate(), app)
        return app.openapi_schema  # type: ignore[return-value]

    app.openapi = openapi  # type: ignore[method-assign]
