"""Default image metadata and pre-check behavior for the Docling backend."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from harborrag_adapters.parsers.compat import DoclingBackend, DoclingBackendOptions
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _plain_pdf() -> bytes:
    import fitz

    document = fitz.open()
    try:
        document.new_page().insert_text((72, 72), "not secret")
        return document.tobytes()
    finally:
        document.close()


def test_docling_backend_parse_omits_image_paths_metadata_by_default():
    document = SimpleNamespace(pages={}, pictures=[], tables=[])

    class _RecordingConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(document=document)

    backend = DoclingBackend(DoclingBackendOptions(converter=_RecordingConverter()))
    result = backend.parse_input(ParseInput(content=_plain_pdf(), filename="plain.pdf"))

    assert "docling_image_paths" not in result.metadata


def test_docling_backend_pre_check_does_not_block_normal_pdfs():
    calls: list[str] = []

    class _RecordingConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("converted")
            return SimpleNamespace(document=SimpleNamespace())

    backend = DoclingBackend(DoclingBackendOptions(converter=_RecordingConverter()))
    backend.parse_input(ParseInput(content=_plain_pdf(), filename="plain.pdf"))

    assert calls == ["converted"]
