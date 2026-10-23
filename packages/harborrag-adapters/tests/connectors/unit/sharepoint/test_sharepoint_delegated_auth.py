"""Unit tests for SharePoint delegated (device code) authentication."""

from __future__ import annotations

import base64
import json

import pytest
from harbor_test_builders import FakeResponse, FakeSession
from sharepoint_http_test_helpers import sharepoint_client

from harborrag_adapters.connectors.exceptions import AuthenticationError
from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig
from harborrag_adapters.connectors.sharepoint.delegated import (
    DELEGATED_SCOPE,
    DEVICE_CODE_GRANT,
    DelegatedTokenCache,
    DeviceCodeLogin,
)
from harborrag_adapters.connectors.sharepoint.permissions import reject_broad_delegated_scopes

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


def _jwt(**claims) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def _cache(tmp_path, *, tenant_id: str = "tid", client_id: str = "cid") -> DelegatedTokenCache:
    return DelegatedTokenCache(
        path=tmp_path / "token.json", tenant_id=tenant_id, client_id=client_id
    )


def _delegated_client(tmp_path, **overrides):
    values = {
        "access_token": None,
        "auth_mode": "device_code",
        "tenant_id": "tid",
        "client_id": "cid",
        "token_cache_path": str(tmp_path / "token.json"),
    }
    values.update(overrides)
    return sharepoint_client(**values)


def test_config_device_code_requires_tenant_and_client(monkeypatch):
    monkeypatch.delenv("MICROSOFT_TENANT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_CLIENT_ID", raising=False)
    with pytest.raises(ValueError, match="requires tenant_id and client_id"):
        SharePointSiteConfig(site_url="https://ex.sharepoint.com/sites/s", auth_mode="device_code")


def test_config_device_code_rejects_client_secret():
    with pytest.raises(ValueError, match="does not use client_secret"):
        SharePointSiteConfig(
            site_url="https://ex.sharepoint.com/sites/s",
            auth_mode="device_code",
            tenant_id="tid",
            client_id="cid",
            client_secret="secret",
        )


def test_config_device_code_ignores_ambient_app_credentials(monkeypatch):
    monkeypatch.setenv("MICROSOFT_GRAPH_TOKEN", "ambient-token")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "ambient-secret")
    config = SharePointSiteConfig(
        site_url="https://ex.sharepoint.com/sites/s",
        auth_mode="device_code",
        tenant_id="tid",
        client_id="cid",
    )
    assert config.access_token is None
    assert config.client_secret is None
    assert config.token_cache_path.endswith("token-tid-cid.json")


@pytest.mark.parametrize("auth_mode", ["Device_Code", " device_code "])
def test_config_device_code_guard_normalizes_auth_mode(monkeypatch, auth_mode):
    monkeypatch.setenv("MICROSOFT_GRAPH_TOKEN", "ambient-token")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "ambient-secret")
    config = SharePointSiteConfig(
        site_url="https://ex.sharepoint.com/sites/s",
        auth_mode=auth_mode,
        tenant_id="tid",
        client_id="cid",
    )
    assert config.auth_mode == "device_code"
    assert config.access_token is None
    assert config.client_secret is None


def test_config_infers_auth_mode_from_credentials():
    token_config = SharePointSiteConfig(
        site_url="https://ex.sharepoint.com/sites/s", access_token="tok"
    )
    app_config = SharePointSiteConfig(
        site_url="https://ex.sharepoint.com/sites/s",
        tenant_id="tid",
        client_id="cid",
        client_secret="secret",
    )
    assert token_config.auth_mode == "access_token"
    assert app_config.auth_mode == "client_credentials"


def test_config_rejects_unknown_auth_mode():
    with pytest.raises(ValueError, match="auth_mode must be one of"):
        SharePointSiteConfig(
            site_url="https://ex.sharepoint.com/sites/s", access_token="tok", auth_mode="basic"
        )


def test_client_redeems_refresh_token_and_stores_rotated_one(tmp_path):
    _cache(tmp_path).save("refresh-1")
    client = _delegated_client(tmp_path)
    client.session = FakeSession(
        responses=[
            FakeResponse(
                status_code=200,
                _json={
                    "access_token": _jwt(scp="Sites.Selected"),
                    "refresh_token": "refresh-2",
                    "expires_in": 3600,
                },
            )
        ]
    )

    token = client._access_token()

    form = client.session.calls[0]["data"]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "refresh-1"
    assert form["scope"] == DELEGATED_SCOPE
    assert "client_secret" not in form
    assert client._access_token() == token  # cached, no second request
    assert _cache(tmp_path).load_refresh_token() == "refresh-2"


def test_client_without_sign_in_explains_how_to_log_in(tmp_path):
    client = _delegated_client(tmp_path)
    with pytest.raises(AuthenticationError, match="No SharePoint sign-in found.*login") as error:
        client._access_token()
    assert str(error.value).count("sharepoint.login") == 1


def test_client_rejects_broad_delegated_scopes(tmp_path):
    _cache(tmp_path).save("refresh-1")
    client = _delegated_client(tmp_path)
    client.session = FakeSession(
        responses=[
            FakeResponse(
                status_code=200,
                _json={"access_token": _jwt(scp="Sites.Read.All offline_access")},
            )
        ]
    )
    with pytest.raises(AuthenticationError, match="Sites.Read.All"):
        client._access_token()


def test_client_expired_refresh_token_asks_for_new_sign_in(tmp_path):
    _cache(tmp_path).save("refresh-1")
    client = _delegated_client(tmp_path)
    client.session = FakeSession(
        responses=[FakeResponse(status_code=400, _json={"error": "invalid_grant"})]
    )
    with pytest.raises(AuthenticationError, match="login"):
        client._access_token()


def test_cache_rejects_token_for_another_app(tmp_path):
    _cache(tmp_path, client_id="other").save("refresh-1")
    with pytest.raises(AuthenticationError, match="another tenant or app"):
        _cache(tmp_path).load_refresh_token()


def test_cache_clear_removes_sign_in(tmp_path):
    cache = _cache(tmp_path)
    cache.save("refresh-1")
    cache.clear()
    assert not cache.path.exists()


def test_device_code_login_polls_until_signed_in(tmp_path):
    session = FakeSession(
        responses=[
            FakeResponse(
                status_code=200,
                _json={
                    "device_code": "dc",
                    "user_code": "ABC",
                    "message": "Go sign in with ABC",
                    "interval": 1,
                    "expires_in": 60,
                },
            ),
            FakeResponse(status_code=400, _json={"error": "authorization_pending"}),
            FakeResponse(
                status_code=200,
                _json={
                    "access_token": _jwt(scp="Sites.Selected"),
                    "id_token": _jwt(preferred_username="user@example.com"),
                    "refresh_token": "refresh-1",
                },
            ),
        ]
    )
    prompts: list[str] = []
    login = DeviceCodeLogin(_cache(tmp_path), session=session, sleep=lambda _s: None)

    account = login.run(prompt=prompts.append)

    assert account == "user@example.com"
    assert prompts == ["Go sign in with ABC"]
    assert session.calls[0]["data"]["scope"] == DELEGATED_SCOPE
    assert session.calls[1]["data"]["grant_type"] == DEVICE_CODE_GRANT
    assert _cache(tmp_path).load_refresh_token() == "refresh-1"


def test_device_code_login_surfaces_identity_error(tmp_path):
    session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"device_code": "dc", "interval": 1}),
            FakeResponse(
                status_code=400,
                _json={
                    "error": "invalid_client",
                    "error_description": "AADSTS7000218: public client flows disabled\nTrace",
                },
            ),
        ]
    )
    login = DeviceCodeLogin(_cache(tmp_path), session=session, sleep=lambda _s: None)
    with pytest.raises(AuthenticationError, match="invalid_client.*AADSTS7000218") as error:
        login.run(prompt=lambda _m: None)
    assert "Trace" not in str(error.value)
    assert not _cache(tmp_path).path.exists()


def test_device_code_login_requires_refresh_token(tmp_path):
    session = FakeSession(
        responses=[
            FakeResponse(status_code=200, _json={"device_code": "dc", "interval": 1}),
            FakeResponse(status_code=200, _json={"access_token": _jwt(scp="Sites.Selected")}),
        ]
    )
    login = DeviceCodeLogin(_cache(tmp_path), session=session, sleep=lambda _s: None)
    with pytest.raises(AuthenticationError, match="offline_access"):
        login.run(prompt=lambda _m: None)


def test_reject_broad_delegated_scopes_allows_sites_selected():
    reject_broad_delegated_scopes(_jwt(scp="Sites.Selected offline_access"))
    reject_broad_delegated_scopes("not-a-jwt")
