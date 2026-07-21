"""Base/mock service pairs and the harbor CLI behave deterministically.

BaseApiController/MockApiController and create_app_state were removed in ST2:
FastAPI routers + create_fastapi_app replaced the controller skeleton.
"""

from __future__ import annotations

import json

import pytest
from harborrag_app.cli.base import BaseCliCommand
from harborrag_app.cli.main import main
from harborrag_app.cli.mock import MockDoctorCommand
from harborrag_app.services.base import BaseAppService
from harborrag_app.services.mock import MockAppService


class BrokenService(BaseAppService):
    """Deliberately calls the abstract bodies to prove they raise."""

    def health(self):
        """Delegate to the abstract base (must raise NotImplementedError)."""
        return super().health()

    def ingest_once(self):
        """Delegate to the abstract base (must raise NotImplementedError)."""
        return super().ingest_once()


class BrokenCli(BaseCliCommand):
    """Deliberately calls the abstract run() to prove it raises."""

    name = "broken"

    def run(self, *, as_json=False):
        """Delegate to the abstract base (must raise NotImplementedError)."""
        return super().run(as_json=as_json)


def test_app_base_methods_raise():
    """Abstract service/CLI bases refuse to run unimplemented methods."""
    with pytest.raises(NotImplementedError):
        BrokenService().health()
    with pytest.raises(NotImplementedError):
        BrokenService().ingest_once()
    with pytest.raises(NotImplementedError):
        BrokenCli().run()


def test_app_mocks_and_cli_outputs(capsys):
    """Mock service reports healthy; doctor/sample-ingest CLI paths emit ok JSON."""
    service = MockAppService()
    assert service.health().ok is True
    ingest = service.ingest_once()
    assert ingest.ok and ingest.data["documents"]
    assert MockDoctorCommand(service).run(as_json=True) == 0
    doctor_payload = json.loads(capsys.readouterr().out)
    assert doctor_payload["ok"] is True
    assert main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert main(["sample-ingest", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
