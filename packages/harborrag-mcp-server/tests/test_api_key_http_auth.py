"""Step 8 acceptance over real HTTP: 401 for every unauthenticated case, revoke is immediate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastmcp import FastMCP

from harborrag_core.ports.api_keys import AuthStoreUnavailable
from harborrag_engine.security import VerifiedKey
from harborrag_mcp_server.server.api_keys import HarborApiKeyVerifier

GOOD = "hrk_dev_v1_good"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


class FakeService:
    def __init__(self) -> None:
        self.valid = {GOOD}
        self.down = False

    async def verify_key(self, raw_key: str) -> VerifiedKey | None:
        if self.down:
            raise AuthStoreUnavailable("down")
        if raw_key not in self.valid:
            return None
        return VerifiedKey(
            key_id="0" * 24,
            tenant_id="engineering",
            owner="user-huy",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


async def _post(service: FakeService, bearer: str | None) -> httpx.Response:
    mcp = FastMCP("t", auth=HarborApiKeyVerifier(service))  # type: ignore[arg-type]
    app = mcp.http_app(path="/mcp")
    headers = dict(HEADERS)
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post("/mcp", json=INIT, headers=headers)


@pytest.mark.asyncio
async def test_valid_key_is_accepted() -> None:
    assert (await _post(FakeService(), GOOD)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("bearer", [None, "wrong-key"])
async def test_missing_header_and_wrong_key_get_401(bearer: str | None) -> None:
    assert (await _post(FakeService(), bearer)).status_code == 401


@pytest.mark.asyncio
async def test_revoked_key_stops_working_without_restart() -> None:
    service = FakeService()
    assert (await _post(service, GOOD)).status_code == 200
    service.valid.clear()  # the CLI revoked the key
    assert (await _post(service, GOOD)).status_code == 401


@pytest.mark.asyncio
async def test_spike_store_outage_never_returns_data() -> None:
    service = FakeService()
    service.down = True
    response = await _post(service, GOOD)
    assert response.status_code in (401, 500, 503)
