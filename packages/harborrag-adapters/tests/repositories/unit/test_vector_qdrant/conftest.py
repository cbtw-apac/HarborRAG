from __future__ import annotations

import pytest

from harborrag_adapters.repositories.vector.qdrant import (
    collections as collections_module,
)
from harborrag_adapters.repositories.vector.qdrant import query as query_module
from harborrag_adapters.repositories.vector.qdrant import (
    repository as repository_module,
)

from .fakes import FakeModels


@pytest.fixture(autouse=True)
def fake_qdrant_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_module, "qm", FakeModels)
    monkeypatch.setattr(collections_module, "qm", FakeModels)
    monkeypatch.setattr(query_module, "qm", FakeModels)
