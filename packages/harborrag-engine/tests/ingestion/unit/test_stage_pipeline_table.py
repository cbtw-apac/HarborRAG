from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from harborrag_core.ingestion.stages import DOCUMENT_STAGE_ORDER
from harborrag_engine.ingestion.release.dependencies import DocumentReleaseDependencies
from harborrag_engine.ingestion.release.pipeline import DocumentStagePipeline

pytestmark = pytest.mark.unit


def test_stage_functions_cover_exactly_the_core_stage_order() -> None:
    pipeline = DocumentStagePipeline(MagicMock(spec=DocumentReleaseDependencies))

    assert set(pipeline.stage_functions()) == set(DOCUMENT_STAGE_ORDER)
