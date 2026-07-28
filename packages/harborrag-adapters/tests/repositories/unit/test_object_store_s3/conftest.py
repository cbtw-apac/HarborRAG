from __future__ import annotations

import pytest

from harborrag_adapters.repositories.object_store.s3 import (
    object_metadata as object_metadata_module,
)
from harborrag_adapters.repositories.object_store.s3 import (
    operations as operations_module,
)
from harborrag_adapters.repositories.object_store.s3 import (
    repository as repository_module,
)

from .fakes import FakeClientError


@pytest.fixture(autouse=True)
def fake_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(object_metadata_module, "ClientError", FakeClientError)
    monkeypatch.setattr(operations_module, "ClientError", FakeClientError)
    monkeypatch.setattr(repository_module, "ClientError", FakeClientError)
