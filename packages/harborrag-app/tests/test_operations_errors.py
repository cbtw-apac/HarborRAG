"""``error_body`` and ``ConnectionRef`` behave the same under any transport."""

import pytest

from harborrag_app.api.auth.principal import Principal
from harborrag_app.operations import ConnectionRef, Scope, TenantForbiddenError, error_body
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborAuthError,
    HarborCapabilityError,
    HarborConfigurationError,
    HarborNotFoundError,
    HarborPreconditionFailedError,
)
from harborrag_runtime.config.connectors.models.spec import ConnectionValidationError, FieldIssue
from harborrag_runtime.errors import RuntimeConnectionError

pytestmark = pytest.mark.unit


def _raised_in_temporal_client() -> RuntimeConnectionError:
    namespace: dict[str, object] = {
        "__name__": "temporalio.client",
        "error": RuntimeConnectionError,
    }
    exec(compile("def connect():\n    raise error('refused')", "client.py", "exec"), namespace)  # noqa: S102
    connect = namespace["connect"]
    assert callable(connect)
    try:
        connect()
    except RuntimeConnectionError as exc:
        return exc
    raise AssertionError("connect did not raise")


CASES = [
    (HarborNotFoundError("x"), 404, "not_found"),
    (HarborAlreadyRunningError("busy"), 409, "already_running"),
    (HarborPreconditionFailedError("stale"), 412, "precondition_failed"),
    (HarborCapabilityError("nope"), 501, "not_supported"),
    (HarborConfigurationError("dsn=secret"), 500, "internal_error"),
    (TenantForbiddenError(), 403, "tenant_forbidden"),
    (ValueError("boom"), 500, "internal_error"),
]


@pytest.mark.parametrize(("exc", "status", "code"), CASES)
def test_error_body_maps_status_and_code(exc: BaseException, status: int, code: str) -> None:
    body = error_body(exc)

    assert (body.status, body.code) == (status, code)


def test_error_body_lists_failing_fields_of_a_connection() -> None:
    exc = ConnectionValidationError(
        [FieldIssue("config.source_path", "must be inside /data/sources")]
    )

    body = error_body(exc)

    assert (body.status, body.code) == (422, "validation_error")
    assert body.details == [
        {"field": "config.source_path", "issue": "must be inside /data/sources"}
    ]


def test_error_body_hides_configuration_text() -> None:
    body = error_body(HarborConfigurationError("dsn=secret"))

    assert "secret" not in body.message
    assert body.details is None


def test_error_body_names_the_lost_dependency() -> None:
    body = error_body(_raised_in_temporal_client())

    assert (body.status, body.code) == (503, "dependency_unavailable")
    assert body.details == {"dependency": "temporal"}


def test_error_body_hides_unknown_failure_text() -> None:
    assert "boom" not in error_body(ValueError("boom")).message


def test_connection_ref_round_trips() -> None:
    ref = ConnectionRef.parse("default/harborrag-workspace")

    assert str(ref) == "default/harborrag-workspace"
    assert ref.key("DEMO").connection_id == "harborrag-workspace"


@pytest.mark.parametrize("value", ["harborrag-workspace", "a/b/c", "/b", "a/ "])
def test_connection_ref_rejects_anything_but_two_parts(value: str) -> None:
    with pytest.raises(ValueError, match="project_id/connection_id"):
        ConnectionRef.parse(value)


def _scope(role: str, tenants: frozenset[str]) -> Scope:
    principal = Principal(subject="cli:alex", role=role, tenant_ids=tenants)  # type: ignore[arg-type]  # parametrised role literal
    return Scope(principal, "DEMO")


def test_scope_require_refuses_a_low_role() -> None:
    with pytest.raises(HarborAuthError):
        _scope("reader", frozenset({"DEMO"})).require("admin")


def test_scope_require_refuses_a_foreign_tenant() -> None:
    with pytest.raises(TenantForbiddenError):
        _scope("owner", frozenset({"OTHER"})).require("reader")


def test_scope_require_accepts_a_wildcard_owner() -> None:
    scope = _scope("owner", frozenset({"*"}))

    scope.require("admin")

    assert (scope.actor, scope.actor_type) == ("cli:alex", "user")
