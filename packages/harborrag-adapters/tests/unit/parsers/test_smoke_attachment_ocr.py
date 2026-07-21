from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from harborrag_adapters.connectors.shared.attachments import FileType

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def _bootstrap() -> dict[str, object]:
    path = Path(__file__).parents[2] / "smoke" / "connectors" / "bootstrap.py"
    return runpy.run_path(str(path))


def test_docling_selection_reuses_rapidocr_for_image_attachments(monkeypatch) -> None:
    monkeypatch.setenv("HARBOR_SMOKE_PDF_BACKEND", "docling")
    monkeypatch.delenv("HARBOR_SMOKE_IMAGE_BACKEND", raising=False)
    scope = _bootstrap()

    custom_parsers = scope["attachment_custom_parsers"]()
    image_parser = custom_parsers[FileType.IMAGE]
    image_parser.__globals__["_rapidocr_engine"] = lambda: (
        lambda _content: SimpleNamespace(txts=("first line", "second line"))
    )

    assert image_parser(b"image bytes", "png") == "first line\nsecond line"


def test_smoke_image_backend_rejects_unknown_values(monkeypatch) -> None:
    monkeypatch.setenv("HARBOR_SMOKE_IMAGE_BACKEND", "unknown")
    scope = _bootstrap()

    with pytest.raises(ValueError, match="choose rapidocr"):
        scope["attachment_custom_parsers"]()


def test_smoke_rapidocr_uses_the_explicit_onnxruntime_dependency(
    monkeypatch, capsys
) -> None:
    created: list[object] = []

    class _RapidOCR:
        def __init__(self) -> None:
            created.append(self)

    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=_RapidOCR))
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(
            get_available_providers=lambda: ["CPUExecutionProvider"],
        ),
    )
    scope = _bootstrap()

    first = scope["_rapidocr_engine"]()
    second = scope["_rapidocr_engine"]()

    assert first is second
    assert created == [first]
    assert "runtime='onnxruntime'" in capsys.readouterr().out


def test_smoke_docling_reports_and_pins_the_resolved_accelerator(
    monkeypatch, capsys
) -> None:
    from harborrag_adapters.parsers import DoclingBackend

    monkeypatch.setenv("HARBOR_SMOKE_PDF_BACKEND", "docling")
    monkeypatch.setenv("HARBOR_SMOKE_DOCLING_DEVICE", "auto")
    monkeypatch.setattr(
        DoclingBackend,
        "resolved_accelerator_device",
        lambda _self: "cuda:0",
    )
    scope = _bootstrap()

    scope["attachment_parser"]()

    output = capsys.readouterr().out
    assert "requested='auto' resolved='cuda:0'" in output
