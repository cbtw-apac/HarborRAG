"""The API imports connectors.yaml at start-up and keeps serving if that fails."""

from __future__ import annotations

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings


class _ImportingApp(MockAppService):
    """Mock app service recording the start-up import."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.imports = 0
        self.fail = fail

    async def import_connectors_once(self) -> int:
        self.imports += 1
        if self.fail:
            raise RuntimeError("database down")
        return 2


@pytest.mark.parametrize("fail", [False, True])
def test_start_up_imports_once_and_survives_a_failure(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    service = _ImportingApp(fail=fail)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))

    with TestClient(create_fastapi_app(ApiSettings())) as client:
        assert client.get("/api/v1/health").status_code == 200

    assert service.imports == 1
