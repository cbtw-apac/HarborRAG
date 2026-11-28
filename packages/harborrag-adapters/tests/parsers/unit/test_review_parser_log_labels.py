"""Parser logs and errors name an input by digest, suffix and size, never by its file name."""

from __future__ import annotations

import logging

import pytest

from harborrag_adapters.parsers import HarborParserFactory
from harborrag_adapters.parsers.common.utils import PARSER_LOGGER_NAME, input_label, name_digest
from harborrag_adapters.parsers.errors import UnreadableImageError, UnsupportedParserError
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit

STEM = "quarterly-plan"
INPUTS = [
    (f"{STEM}.txt", b"Plan for the quarter.", "text/plain"),
    (f"{STEM}.md", b"# Plan\n\nShip the release.", "text/markdown"),
    (f"{STEM}.csv", b"team,goal\nplatform,release\n", "text/csv"),
    (f"{STEM}.json", b'{"goal": "release"}', "application/json"),
    (f"{STEM}.html", b"<p>Ship the release.</p>", "text/html"),
]


def _logged_values(caplog: pytest.LogCaptureFixture) -> str:
    """Return every message and record attribute logged at INFO or above."""
    return "\n".join(
        f"{record.getMessage()} {record.__dict__!r}"
        for record in caplog.records
        if record.levelno >= logging.INFO
    )


@pytest.mark.parametrize(("filename", "content", "content_type"), INPUTS)
def test_default_parse_path_logs_no_file_name(
    caplog: pytest.LogCaptureFixture, filename: str, content: bytes, content_type: str
) -> None:
    registry = HarborParserFactory().create_registry()

    with caplog.at_level(logging.INFO, logger=PARSER_LOGGER_NAME):
        registry.parse(ParseInput(content=content, filename=filename, content_type=content_type))

    logged = _logged_values(caplog)
    assert logged
    assert STEM not in logged
    assert name_digest(filename) in logged


def test_unreadable_image_warning_and_error_carry_no_file_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = HarborParserFactory().create_registry()
    parse_input = ParseInput(
        content=b"not an image", filename=f"{STEM}.png", content_type="image/png"
    )

    with caplog.at_level(logging.INFO), pytest.raises(UnreadableImageError) as raised:
        registry.parse(parse_input)

    assert STEM not in str(raised.value)
    assert STEM not in _logged_values(caplog)


def test_input_label_names_digest_suffix_and_size() -> None:
    parse_input = ParseInput(content=b"12345", filename=f"{STEM}.pdf")

    assert input_label(parse_input) == f"input={name_digest(f'{STEM}.pdf')} suffix=.pdf size=5"


def test_unsupported_parser_error_keeps_the_name_as_an_attribute_only() -> None:
    error = UnsupportedParserError(filename=f"{STEM}.xyz", mime_type="application/x-unknown")

    assert STEM not in str(error)
    assert ".xyz" in str(error)
    assert error.filename == f"{STEM}.xyz"
