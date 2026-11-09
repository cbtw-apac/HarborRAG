"""The API seeds config/settings.yaml at start-up and keeps serving if that fails."""

from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings


class _SeedingApp(MockAppService):
    """Mock app service recording the start-up settings seed."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.seeds = 0
        self.fail = fail

    async def seed_setting_overrides_once(self) -> int:
        self.seeds += 1
        if self.fail:
            raise RuntimeError("database down")
        return 3


@pytest.mark.parametrize("fail", [False, True])
def test_start_up_seeds_once_and_survives_a_failure(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    service = _SeedingApp(fail=fail)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))

    with TestClient(create_fastapi_app(ApiSettings())) as client:
        assert client.get("/healthz").status_code == 200

    assert service.seeds == 1


def test_a_service_without_the_method_is_skipped_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A composition mode predating this feature (no `seed_setting_overrides_once`) must not crash."""
    service = MockAppService()
    assert not hasattr(service, "seed_setting_overrides_once")
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))

    with TestClient(create_fastapi_app(ApiSettings())) as client:
        assert client.get("/healthz").status_code == 200
