"""The document stage order and the raw-document loader seam stay in step with their users."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_core.ingestion.stages import DOCUMENT_STAGE_ORDER
from harborrag_core.testing.fakes import FakeConnector
from harborrag_engine.ingestion.release.dependencies import RawDocumentLoader
from harborrag_runtime.document_stage_catalog import DOCUMENT_STAGE_CATALOG

pytestmark = pytest.mark.unit


def test_document_stage_catalog_names_follow_core_stage_order() -> None:
    assert tuple(spec.name for spec in DOCUMENT_STAGE_CATALOG) == DOCUMENT_STAGE_ORDER


def _as_loader_from_base_connector(connector: BaseConnector) -> RawDocumentLoader:
    return connector


def _as_loader_from_harbor_connector(connector: HarborConnector) -> RawDocumentLoader:
    return connector


def _as_loader_from_fake_connector(connector: FakeConnector) -> RawDocumentLoader:
    return connector


def test_connectors_satisfy_raw_document_loader_for_mypy() -> None:
    assert callable(_as_loader_from_base_connector)
    assert callable(_as_loader_from_harbor_connector)
    assert callable(_as_loader_from_fake_connector)
