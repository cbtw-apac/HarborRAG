from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from pydantic import ValidationError

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.hmac import HmacTokenVerifier
from harborrag_app.api.settings import ApiSettings

pytestmark = pytest.mark.unit

_AUTH_SECRET = "auth-secret-for-tests-at-least-32-bytes-Tail-9c1d"
_SECRET_TAIL = _AUTH_SECRET[-4:]


def test_failed_settings_check_keeps_the_auth_secret_out_of_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_AUTH_SECRET", _AUTH_SECRET)

    with pytest.raises(ValidationError) as caught:
        ApiSettings(port=8000, ops_port=8000)

    message = str(caught.value)
    assert "HARBORRAG_OPS_PORT" in message
    assert "input_value" not in message
    assert _SECRET_TAIL not in message


@pytest.mark.parametrize("token", ["", "   "])
def test_blank_scrape_token_is_no_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("HARBORRAG_METRICS_SCRAPE_TOKEN", token)

    assert ApiSettings().metrics_scrape_token is None


@pytest.mark.parametrize("authorization", ["Bearer ", "Bearer", "Bearer    "])
def test_blank_scrape_token_refuses_an_empty_bearer(
    monkeypatch: pytest.MonkeyPatch, authorization: str
) -> None:
    monkeypatch.setenv("HARBORRAG_METRICS_SCRAPE_TOKEN", "")
    monkeypatch.setattr(api_app, "select_app_service", lambda: (MockAppService(), "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=_AUTH_SECRET, ops_port=9465)

    with TestClient(create_fastapi_app(settings)) as client:
        response = client.get(
            "http://testserver:9465/metrics", headers={"Authorization": authorization}
        )

    assert response.status_code == 401


def test_hmac_verifier_repr_hides_the_signing_secret() -> None:
    verifier = HmacTokenVerifier(secret=_AUTH_SECRET)

    assert _SECRET_TAIL not in repr(verifier)
