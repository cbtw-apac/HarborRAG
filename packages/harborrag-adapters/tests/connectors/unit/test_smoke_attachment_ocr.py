from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harborrag_adapters.connectors.attachments.processing import FileType
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def _bootstrap() -> dict[str, object]:
    path = Path(__file__).parents[2] / "smoke" / "connectors" / "bootstrap.py"
    return runpy.run_path(str(path))


def test_attachment_custom_parsers_routes_images_to_rapidocr() -> None:
    scope = _bootstrap()

    custom_parsers = scope["attachment_custom_parsers"]()
    image_parser = custom_parsers[FileType.IMAGE]
    image_parser.__globals__["_rapidocr_engine"] = lambda: (
        lambda _content: SimpleNamespace(txts=("first line", "second line"))
    )

    assert image_parser(b"image bytes", "png") == "first line\nsecond line"


def test_rapid_ocr_image_parser_returns_a_parsed_document() -> None:
    scope = _bootstrap()

    parser = scope["RapidOcrImageParser"]()
    parser.parse.__globals__["_rapidocr_engine"] = lambda: (
        lambda _content: SimpleNamespace(txts=("hello", "world"))
    )

    document = parser.parse(ParseInput(content=b"image bytes", filename="scan.png"))

    assert document.content == "hello\nworld"
    assert document.parser_name == "image"
    assert document.elements and document.elements[0].type == "image"


def test_smoke_rapidocr_uses_the_explicit_onnxruntime_dependency(monkeypatch, capsys) -> None:
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


def test_build_harbor_parser_uses_docling_for_pdf_and_rapidocr_for_images() -> None:
    scope = _bootstrap()

    harbor_parser = scope["build_harbor_parser"]()

    pdf_parser = harbor_parser.create("pdf")
    assert [backend.name for backend in pdf_parser.backends] == ["docling"]

    image_parser = harbor_parser.create("image")
    assert image_parser.parser_engine == "rapidocr"


def test_rapid_ocr_image_parser_normalizes_suffixes_for_route_matching() -> None:
    # Regression guard: a plain (non-BaseParser) class keeps dot-less suffixes
    # like "png", but `ParseInput.suffix` is always dotted (".png"). Local
    # files have no content_type, so suffix routing is the only way they ever
    # reach this parser — silently dropping the dot breaks it with no error
    # until someone runs a real local image through `HarborParser.parse`.
    scope = _bootstrap()
    parser = scope["RapidOcrImageParser"]()
    assert all(suffix.startswith(".") for suffix in parser.suffixes)


def test_build_harbor_parser_routes_a_local_image_by_suffix_alone() -> None:
    scope = _bootstrap()
    harbor_parser = scope["build_harbor_parser"]()

    resolved = harbor_parser.parser_for(ParseInput(content=b"image bytes", filename="scan.png"))

    assert resolved is not None
    assert resolved.name == "image"
    assert resolved.parser_engine == "rapidocr"
