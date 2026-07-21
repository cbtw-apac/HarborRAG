"""Unit tests for the PaddleOCR PDF backend."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from harborrag_adapters.parsers import PaddleOcrBackend, PaddleOcrBackendOptions

pytestmark = pytest.mark.unit


@pytest.mark.whitebox
def test_paddleocr_pipeline_options_are_sparse_and_advanced():
    backend = PaddleOcrBackend(
        PaddleOcrBackendOptions(
            lang="en",
            device="cpu",
            cpu_threads=2,
            use_table_recognition=False,
            markdown_ignore_labels=("image", "footer"),
        )
    )

    assert backend._pipeline_options() == {
        "lang": "en",
        "device": "cpu",
        "cpu_threads": 2,
        "use_table_recognition": False,
        "markdown_ignore_labels": ["image", "footer"],
    }


class _FailingPipeline:
    def __init__(self, **_options: Any) -> None:
        raise RuntimeError("missing model weights")


class _WorkingPipeline:
    def __init__(self, **_options: Any) -> None:
        pass

    def predict(self, input: str) -> list[dict[str, str]]:
        return [{"markdown": f"parsed {input}"}]

    def concatenate_markdown_pages(self, pages: list[str]) -> str:
        return "\n".join(pages)


@pytest.mark.whitebox
def test_paddleocr_falls_back_when_pipeline_construction_raises():
    fake_module = SimpleNamespace(
        PPStructureV3=_FailingPipeline,
        PaddleOCRVL=_WorkingPipeline,
        PPStructure=_WorkingPipeline,
    )
    backend = PaddleOcrBackend()

    result, warnings = backend._predict(fake_module, "doc.pdf")

    assert result == "parsed doc.pdf"
    assert backend._active_pipeline_class == "PaddleOCRVL"
    assert any("PPStructureV3" in warning for warning in warnings)


class _FailingPredictPipeline:
    def __init__(self, **_options: Any) -> None:
        pass

    def predict(self, input: str) -> Any:
        raise RuntimeError("GPU init failure")


@pytest.mark.whitebox
def test_paddleocr_falls_back_when_predict_raises():
    fake_module = SimpleNamespace(
        PPStructureV3=_FailingPredictPipeline,
        PaddleOCRVL=_WorkingPipeline,
        PPStructure=_WorkingPipeline,
    )
    backend = PaddleOcrBackend()

    result, warnings = backend._predict(fake_module, "doc.pdf")

    assert result == "parsed doc.pdf"
    assert backend._active_pipeline_class == "PaddleOCRVL"
    assert any("GPU init failure" in warning for warning in warnings)


class _NoneCombinerPipeline:
    def __init__(self, **_options: Any) -> None:
        pass

    def predict(self, input: str) -> list[dict[str, str]]:
        return [{"markdown": f"parsed {input}"}]

    def concatenate_markdown_pages(self, pages: list[str]) -> None:
        return None


@pytest.mark.whitebox
def test_paddleocr_falls_back_to_raw_output_when_combiner_returns_none():
    """A markdown combiner returning None must not become the literal string
    "None" (which `str(None)` would produce) -- it must fall through to the
    raw predict() output instead."""
    fake_module = SimpleNamespace(
        PPStructureV3=_NoneCombinerPipeline,
        PaddleOCRVL=_WorkingPipeline,
        PPStructure=_WorkingPipeline,
    )
    backend = PaddleOcrBackend()

    result, _warnings = backend._predict(fake_module, "doc.pdf")

    assert result == [{"markdown": "parsed doc.pdf"}]


@pytest.mark.whitebox
def test_paddleocr_falls_back_to_legacy_api_when_all_pipelines_fail():
    class _LegacyOcr:
        def __init__(self, **_options: Any) -> None:
            pass

        def ocr(self, path: str, cls: bool = True) -> list[str]:
            return [f"legacy {path}"]

    fake_module = SimpleNamespace(
        PPStructureV3=_FailingPipeline,
        PaddleOCRVL=_FailingPipeline,
        PPStructure=_FailingPipeline,
        PaddleOCR=_LegacyOcr,
    )
    backend = PaddleOcrBackend()

    result, warnings = backend._predict(fake_module, "doc.pdf")

    assert result == ["legacy doc.pdf"]
    assert backend._active_pipeline_class == "PaddleOCR"
    assert len(warnings) == 3
