"""SharePoint throttles through the runtime's shared connector rate limiter."""

from __future__ import annotations

import pytest
from harbor_test_builders import FakeResponse, FakeSession
from sharepoint_test_helpers import config

from harborrag_adapters.connectors import SharePointConnector, connector_registry
from harborrag_adapters.connectors.rate_limiting import RateLimitScope

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


class _RecordingLimiter:
    def __init__(self) -> None:
        self.acquired: list[tuple[RateLimitScope, int]] = []

    def acquire(self, scope: RateLimitScope, *, requests_per_minute: int) -> None:
        self.acquired.append((scope, requests_per_minute))

    def close(self) -> None:
        pass


def test_the_sharepoint_provider_receives_the_runtime_rate_limiter():
    definition = connector_registry.get_definition("sharepoint")

    assert dict(definition.constructor_dependencies) == {"rate_limiter": "rate_limiter"}


def test_graph_requests_acquire_the_shared_limiters_graph_lane():
    limiter = _RecordingLimiter()
    connector = SharePointConnector(
        config(
            access_token=None,
            tenant_id="tenant-1",
            client_id="app-1",
            client_secret="super-secret",
            requests_per_minute=90,
        ),
        rate_limiter=limiter,
    )
    client = connector.client
    client._token, client._token_expires_at = "token", float("inf")
    client.session = FakeSession(responses=[FakeResponse(status_code=200, _json={"id": "s"})])

    client.get_json("sites/s")

    [(scope, requests_per_minute)] = limiter.acquired
    assert requests_per_minute == 90
    assert (scope.connector_type, scope.source_host, scope.api_family) == (
        "sharepoint",
        "graph.microsoft.com",
        "graph",
    )
    key = scope.redis_key("prefix")
    assert "super-secret" not in key and "tenant-1" not in key and "app-1" not in key


def test_connectors_of_one_app_registration_share_a_lane():
    def scope(**overrides):
        limiter = _RecordingLimiter()
        values = {"access_token": None, "tenant_id": "t", "client_id": "c", "client_secret": "s"}
        connector = SharePointConnector(config(**{**values, **overrides}), rate_limiter=limiter)
        connector.client._acquire()
        return limiter.acquired[0][0]

    assert scope(site_url="https://contoso.sharepoint.com/sites/A") == scope(
        site_url="https://contoso.sharepoint.com/sites/B"
    )
    assert scope() != scope(client_id="other-app")
