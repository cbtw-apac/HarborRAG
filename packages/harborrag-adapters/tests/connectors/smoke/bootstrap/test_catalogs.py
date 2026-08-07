"""Unit tests for the smoke-test connector bootstrap helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from bootstrap import catalogs
from harborrag_runtime.config import (
    ConnectorCatalog,
    ConnectorConfigurationError,
    ConnectorDefinition,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_build_connector_refuses_disabled_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled_catalog = ConnectorCatalog(
        connectors={
            "local-docs": ConnectorDefinition(
                name="local-docs",
                provider="local",
                enabled=False,
                settings={"source_path": "."},
            ),
        },
        source_path=Path("connectors.yaml"),
        version=1,
    )
    monkeypatch.setattr(catalogs, "connector_catalog", lambda: disabled_catalog)

    with pytest.raises(ConnectorConfigurationError, match="disabled"):
        catalogs.build_connector("local-docs", include_attachments=False)
