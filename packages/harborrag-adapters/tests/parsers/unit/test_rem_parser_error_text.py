"""Non-PDF parser errors name no file, path or document bytes."""

from __future__ import annotations

import io
import logging
import traceback
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from harborrag_adapters.parsers import HarborParserFactory
from harborrag_adapters.parsers.common.resources import read_parse_input_bytes
from harborrag_adapters.parsers.common.utils import PARSER_LOGGER_NAME
from harborrag_adapters.parsers.common.validation import (
    open_guarded_zip,
    parse_input_is_empty,
    wrap_parse_errors,
)
from harborrag_adapters.parsers.document.engines.msword.engine import (
    MsWordBinaryDocumentEngine,
)
from harborrag_adapters.parsers.errors import ParseError
from harborrag_adapters.parsers.presentation.engines.msppt.engine import (
    MsPowerPointBinaryPresentationEngine,
)
from harborrag_adapters.parsers.spreadsheet.engines.openpyxl.rendering import (
    xlrd_errors_as_parse_errors,
)
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit

STEM = "demo-design-notes"
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _full_text(error: BaseException) -> str:
    """Return the error as a traceback would print it, causes included."""
    return "".join(traceback.format_exception(error))


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)


def test_missing_path_input_error_and_cause_name_no_path(tmp_path: Path) -> None:
    missing = ParseInput(path=tmp_path / f"{STEM}.json")

    with pytest.raises(ParseError) as raised:
        read_parse_input_bytes(missing)

    assert STEM not in _full_text(raised.value)


def test_empty_check_on_a_missing_path_keeps_the_errno_without_the_path(tmp_path: Path) -> None:
    missing = ParseInput(path=tmp_path / f"{STEM}.pdf")

    with pytest.raises(FileNotFoundError) as raised:
        parse_input_is_empty(missing)

    assert STEM not in _full_text(raised.value)


def test_wrapped_library_error_names_its_class_only() -> None:
    with pytest.raises(ParseError) as raised, wrap_parse_errors("docx2txt"):
        raise OSError(2, "No such file or directory", f"/srv/inbox/{STEM}.docx")

    assert str(raised.value) == "docx2txt failed to parse input: FileNotFoundError"


def test_archive_bomb_error_names_no_member() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"word/media/{STEM}.png", b"\0" * 1_000_000)

    with pytest.raises(ParseError, match="compression ratio") as raised:
        open_guarded_zip(buffer.getvalue())

    assert STEM not in str(raised.value)


@pytest.mark.parametrize(
    ("engine", "reader", "suffix"),
    [
        (MsWordBinaryDocumentEngine, "_read_stories", "doc"),
        (MsPowerPointBinaryPresentationEngine, "_read_slides", "ppt"),
    ],
)
def test_legacy_office_decode_error_names_its_class_only(
    monkeypatch: pytest.MonkeyPatch, engine: type, reader: str, suffix: str
) -> None:
    def _fail(*_args: object) -> None:
        raise ValueError(f"bad record in {STEM}: Alex Example, Senior engineer")

    monkeypatch.setattr(engine, reader, staticmethod(_fail))
    parse_input = ParseInput(content=OLE_SIGNATURE + b"\0" * 504, filename=f"{STEM}.{suffix}")

    with pytest.raises(ParseError) as raised:
        engine().parse(parse_input)

    assert str(raised.value).endswith(": ValueError")
    assert STEM not in str(raised.value)
    assert "Alex Example" not in str(raised.value)


def test_xlrd_error_names_its_class_only() -> None:
    class XLRDError(Exception):
        pass

    with pytest.raises(ParseError) as raised:
        with xlrd_errors_as_parse_errors(SimpleNamespace(XLRDError=XLRDError)):
            raise XLRDError("Expected BOF record; found b'Alex Exa'")

    assert str(raised.value) == "xlrd failed to parse input: XLRDError"


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        (f"{STEM}.json", b'{"goal": ', "application/json"),
        (f"{STEM}.csv", b'team,goal\n"platform,release\n', "text/csv"),
    ],
)
def test_json_and_csv_failures_name_no_file(
    caplog: pytest.LogCaptureFixture, filename: str, content: bytes, content_type: str
) -> None:
    registry = HarborParserFactory().create_registry()
    parse_input = ParseInput(content=content, filename=filename, content_type=content_type)

    with caplog.at_level(logging.DEBUG, logger=PARSER_LOGGER_NAME):
        try:
            registry.parse(parse_input)
        except ParseError as error:
            assert STEM not in _full_text(error)

    assert caplog.records
    assert STEM not in _logged(caplog)
