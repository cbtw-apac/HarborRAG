"""Graph requests always carry a live token, and the refresh-token cache survives races."""

from __future__ import annotations

import json
import os
import stat
import threading
from typing import Any

import pytest
from harbor_test_builders import FakeResponse
from sharepoint_http_test_helpers import client_credentials_sharepoint_client, sharepoint_client

from harborrag_adapters.connectors.exceptions import AuthenticationError
from harborrag_adapters.connectors.sharepoint.delegated import DelegatedTokenCache

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _RecordingSession:
    """Replays scripted responses and snapshots each request's Authorization header."""

    def __init__(self, gets: list[Any], tokens: list[str]) -> None:
        self.headers: dict[str, str] = {}
        self.gets = gets
        self.tokens = tokens
        self.sent: list[str] = []
        self.token_requests = 0

    def request(self, method: str, url: str, *, headers: dict[str, str], **_kwargs: Any):
        self.sent.append(headers["Authorization"])
        return self.gets.pop(0)

    def post(self, url: str, **_kwargs: Any):
        self.token_requests += 1
        token = self.tokens.pop(0)
        return FakeResponse(status_code=200, _json={"access_token": token, "expires_in": 150})

    def close(self) -> None:
        pass


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    import time

    fake = _Clock()
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    monkeypatch.setattr(time, "sleep", fake.sleep)
    return fake


def test_a_retry_after_a_long_retry_after_wait_sends_a_fresh_token(clock):
    client = client_credentials_sharepoint_client()
    client.session = _RecordingSession(
        gets=[
            FakeResponse(status_code=429, headers={"Retry-After": "120"}),
            FakeResponse(status_code=200, _json={"ok": True}),
        ],
        tokens=["TOKEN-A", "TOKEN-B"],
    )

    assert client.get_json("sites/x") == {"ok": True}

    # TOKEN-A had 150s to live: the 120s wait left it inside the 60s refresh margin.
    assert client.session.sent == ["Bearer TOKEN-A", "Bearer TOKEN-B"]


def test_a_401_refreshes_the_token_once_and_retries(clock):
    client = client_credentials_sharepoint_client()
    client.session = _RecordingSession(
        gets=[
            FakeResponse(status_code=401, text="InvalidAuthenticationToken"),
            FakeResponse(status_code=200, _json={"ok": True}),
        ],
        tokens=["REVOKED", "FRESH"],
    )

    assert client.get_json("sites/x") == {"ok": True}
    assert client.session.sent == ["Bearer REVOKED", "Bearer FRESH"]


def test_a_401_refresh_does_not_need_a_spare_retry(clock):
    client = client_credentials_sharepoint_client(max_retries=0)
    client.session = _RecordingSession(
        gets=[
            FakeResponse(status_code=401, text="expired"),
            FakeResponse(status_code=200, _json={"ok": True}),
        ],
        tokens=["OLD", "NEW"],
    )

    assert client.get_json("sites/x") == {"ok": True}


def test_a_second_401_after_refreshing_is_a_real_authentication_failure(clock):
    client = client_credentials_sharepoint_client(max_retries=3)
    client.session = _RecordingSession(
        gets=[FakeResponse(status_code=401, text="no"), FakeResponse(status_code=401, text="no")],
        tokens=["ONE", "TWO"],
    )

    with pytest.raises(AuthenticationError):
        client.get_json("sites/x")
    assert client.session.sent == ["Bearer ONE", "Bearer TWO"]
    assert client.session.token_requests == 2


def test_a_configured_access_token_is_never_refreshed_on_401(clock):
    client = sharepoint_client(access_token="configured")
    client.session = _RecordingSession(gets=[FakeResponse(status_code=401, text="no")], tokens=[])

    with pytest.raises(AuthenticationError):
        client.get_json("sites/x")
    assert client.session.sent == ["Bearer configured"]


def _cache(path) -> DelegatedTokenCache:
    return DelegatedTokenCache(path=path, tenant_id="tid", client_id="cid")


def test_concurrent_refresh_token_saves_never_fail_or_corrupt_the_cache(tmp_path):
    path = tmp_path / "token.json"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def save(worker: int) -> None:
        start.wait()
        try:
            for round_ in range(40):
                _cache(path).save("r" * (1000 + worker * 97 + round_))
                assert _cache(path).load_refresh_token()
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    json.loads(path.read_text(encoding="utf-8"))
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["token.json"]


def test_save_never_writes_through_a_pre_created_temporary_file(tmp_path):
    path = tmp_path / "token.json"
    victim = tmp_path / "readable-by-attacker.txt"
    victim.write_text("untouched", encoding="utf-8")
    # The old fixed temporary name, planted by another local user.
    (tmp_path / "token.json.tmp").symlink_to(victim)

    _cache(path).save("secret-refresh-token")

    assert victim.read_text(encoding="utf-8") == "untouched"
    assert _cache(path).load_refresh_token() == "secret-refresh-token"
    assert not path.is_symlink()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
