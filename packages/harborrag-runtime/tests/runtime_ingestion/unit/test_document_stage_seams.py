"""The document stage catalog stays in step with the core stage order."""

from __future__ import annotations

import pytest

from harborrag_core.ingestion.stages import DOCUMENT_STAGE_ORDER
from harborrag_runtime.document_stage_catalog import DOCUMENT_STAGE_CATALOG

pytestmark = pytest.mark.unit


def test_document_stage_catalog_names_follow_core_stage_order() -> None:
    assert tuple(spec.name for spec in DOCUMENT_STAGE_CATALOG) == DOCUMENT_STAGE_ORDER
