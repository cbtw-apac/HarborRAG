from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from harbor_test_builders import (  # noqa: E402  (path set above)
    build_docx_bytes,
    build_odt_bytes,
    build_zip_bomb_bytes,
)


@pytest.fixture
def docx_bytes() -> bytes:
    return build_docx_bytes()


@pytest.fixture
def odt_bytes() -> bytes:
    return build_odt_bytes()


@pytest.fixture
def zip_bomb_bytes() -> bytes:
    return build_zip_bomb_bytes()


@pytest.fixture(autouse=True)
def _hermetic_connector_host_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connector config validation resolves DNS names; answer with a public address.

    Literal IP hosts never reach this seam, so private-literal checks still run for
    real. Tests that need a specific answer override ``_resolve_host`` themselves.
    """

    monkeypatch.setattr(
        "harborrag_adapters.connectors.policies.validation._resolve_host",
        lambda _hostname, _port: ("1.1.1.1",),
    )
