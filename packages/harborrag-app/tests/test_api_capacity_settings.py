"""Per-tenant capacity configuration, scope derivation, and lease lifetime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import ApiCapacityDependency, capacity_scope_for
from harborrag_app.api.capacity_scope import SHARED_TENANT_KEY, CapacityScope
from harborrag_app.api.settings import ApiSettings


def _principal(*, tenants: frozenset[str], user_id: str = "") -> Principal:
    return Principal(
        subject="service-credential",
        role="owner",
        tenant_ids=tenants,
        user_id=user_id,
    )


def test_default_capacity_limits_give_the_tenant_room_for_several_users() -> None:
    limits = ApiSettings().default_capacity_limits()

    assert limits.user == limits.principal
    assert limits.user.requests_per_minute == 60
    assert limits.user.max_inflight == 4
    assert limits.tenant.requests_per_minute == 600
    assert limits.tenant.max_inflight == 40


def test_a_tenant_override_inherits_every_tier_it_does_not_set() -> None:
    settings = ApiSettings(
        api_tenant_capacity_overrides={
            "enterprise": {"user_requests_per_minute": 120, "tenant_requests_per_minute": 6_000}
        }
    )

    overrides = settings.tenant_capacity_limits()

    assert set(overrides) == {"enterprise"}
    enterprise = overrides["enterprise"]
    assert enterprise.user.requests_per_minute == 120
    assert enterprise.tenant.requests_per_minute == 6_000
    # Unset tiers keep the global default, and no other tenant is affected.
    assert enterprise.principal == settings.default_capacity_limits().principal
    assert enterprise.tenant.max_inflight == 40


def test_capacity_overrides_load_from_a_json_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HARBORRAG_API_TENANT_CAPACITY_OVERRIDES",
        '{"enterprise": {"tenant_max_inflight": 80}}',
    )

    assert ApiSettings().tenant_capacity_limits()["enterprise"].tenant.max_inflight == 80


@pytest.mark.parametrize(
    "override",
    [
        {"tenant_requests_per_minutes": 900},
        {"requests_per_minute": 0},
        {"tenant_max_inflight": 100_000},
        {"tenant_requests_per_minute": 10},
        {"tenant_max_inflight": 1},
    ],
)
def test_invalid_tenant_overrides_are_rejected_naming_the_tenant(
    override: dict[str, int],
) -> None:
    with pytest.raises(ValidationError, match="enterprise"):
        ApiSettings(api_tenant_capacity_overrides={"enterprise": override})


def test_a_tenant_ceiling_below_one_members_share_is_unreachable_config() -> None:
    with pytest.raises(ValidationError, match="per-tenant request rate"):
        ApiSettings(api_requests_per_minute_per_tenant=30)
    with pytest.raises(ValidationError, match="per-tenant concurrency ceiling"):
        ApiSettings(api_max_inflight_per_tenant=2)


def test_capacity_scope_charges_a_single_tenant_credential_to_that_tenant() -> None:
    scope = capacity_scope_for(_principal(tenants=frozenset({"acme"}), user_id="alice"))

    assert scope == CapacityScope(
        tenant_id="acme",
        principal_id="service-credential",
        user_id="alice",
    )


@pytest.mark.parametrize(
    "tenants",
    [frozenset({"*"}), frozenset({"acme", "globex"}), frozenset()],
)
def test_wildcard_and_multi_tenant_credentials_share_one_aggregate_pool(
    tenants: frozenset[str],
) -> None:
    """Adding a second tenant id must not mint a private tenant ceiling."""

    assert capacity_scope_for(_principal(tenants=tenants)).tenant_id == SHARED_TENANT_KEY


def test_capacity_scope_falls_back_to_the_subject_without_a_user_id_claim() -> None:
    scope = capacity_scope_for(_principal(tenants=frozenset({"acme"})))

    assert scope.user_id == scope.principal_id == "service-credential"


class _RecordingLimiter:
    """Record reserve/release ordering against a streamed response body."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def reserve(self, scope: CapacityScope) -> str:
        self.events.append("reserve")
        return "lease-1"

    async def release(self, scope: CapacityScope, lease_id: str) -> None:
        self.events.append(f"release:{lease_id}")

    async def aclose(self) -> None:
        return None


def test_a_streamed_body_holds_its_lease_while_the_handler_deadline_is_disarmed() -> None:
    events: list[str] = []
    app = FastAPI()
    app.state.settings = ApiSettings(api_request_timeout_seconds=1.0)
    app.state.api_capacity_limiter = _RecordingLimiter(events)

    @app.get("/stream")
    async def stream(_lease: ApiCapacityDependency) -> StreamingResponse:
        async def frames() -> AsyncIterator[bytes]:
            for index in range(2):
                # Together the frames outlive api_request_timeout_seconds: a
                # request-scoped deadline would cancel the body mid-flight.
                await asyncio.sleep(0.6)
                events.append(f"frame-{index}")
                yield f"frame-{index}\n".encode()

        return StreamingResponse(frames(), media_type="text/event-stream")

    with TestClient(app) as client:
        response = client.get("/stream")

    assert response.status_code == 200
    assert response.text == "frame-0\nframe-1\n"
    assert events == ["reserve", "frame-0", "frame-1", "release:lease-1"]
