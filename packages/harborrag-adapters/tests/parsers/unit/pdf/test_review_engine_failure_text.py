"""A PDF engine failure is reported by its class, not by text that quotes the file (CON-1)."""

from __future__ import annotations

import logging
from typing import ClassVar

import pytest

from harborrag_adapters.parsers.compat import PARSER_LOGGER_NAME, PdfBackend, PdfParser
from harborrag_adapters.parsers.errors import PDFParsingFailedError
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit

CV_PATH = "/srv/uploads/Alex_Example_CV.pdf"


class _MissingFileBackend(PdfBackend):
    """Fails the way a library does when the file it was handed is gone."""

    name: ClassVar[str] = "missing_file_pdf"

    def parse_input(self, input: ParseInput) -> object:
        raise FileNotFoundError(2, "No such file or directory", CV_PATH)


def test_engine_failure_text_never_reaches_the_error_or_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PARSER_LOGGER_NAME)
    parser = PdfParser(backends=[_MissingFileBackend()])

    with pytest.raises(PDFParsingFailedError) as raised:
        parser.parse_input(ParseInput(content=b"%PDF", filename="report.pdf"))

    assert "missing_file_pdf (failed (FileNotFoundError))" in str(raised.value)
    assert "Alex_Example" not in str(raised.value)
    assert "Alex_Example" not in caplog.text
