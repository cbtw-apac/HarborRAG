"""Unit tests for the legacy PowerPoint 97-2003 binary (`.ppt`) engine."""

from __future__ import annotations

import pytest
from cfb_writer import build_compound_file
from harbor_test_builders import build_pptx_bytes
from legacy_office_builders import PptSlide, build_doc_bytes, build_ppt_bytes

from harborrag_adapters.parsers.compat import PptParser
from harborrag_adapters.parsers.errors import (
    ParseError,
    PasswordProtectedError,
    UnsupportedFormatError,
)
from harborrag_adapters.parsers.factory import HarborParserFactory
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.graybox]
pytest.importorskip("olefile", reason="legacy Office parser tests require the parsers extra")

MASTER_PROMPT = "Click to edit Master title style"


def _parse(data: bytes, filename: str = "deck.ppt"):
    return PptParser().parse(ParseInput(content=data, filename=filename))


def _deck() -> bytes:
    return build_ppt_bytes(
        [
            PptSlide("Quarterly Review", "Doanh thu tăng trưởng — Nguyễn Văn Á", "Mention Q3"),
            PptSlide("Roadmap", "План развития"),
            PptSlide(""),
        ],
        master_text=MASTER_PROMPT,
    )


def test_slide_text_is_extracted_one_element_per_slide() -> None:
    document = _parse(_deck())

    assert document.parser_name == "ppt"
    assert document.metadata["slide_count"] == 3
    slides = [element for element in document.elements if "part" not in element.metadata]
    assert [element.id for element in slides] == ["ppt:slide:1", "ppt:slide:2"]
    assert slides[0].content == "Quarterly Review\nDoanh thu tăng trưởng — Nguyễn Văn Á"
    assert slides[1].content == "Roadmap\nПлан развития"
    assert slides[1].metadata == {"slide": 2}
    assert document.content.startswith("Slide 1\nQuarterly Review")


def test_master_placeholder_text_is_not_emitted() -> None:
    document = _parse(_deck())
    assert MASTER_PROMPT not in document.content
    assert all(MASTER_PROMPT not in element.content for element in document.elements)


def test_speaker_notes_are_a_separate_element() -> None:
    document = _parse(_deck())

    notes = [element for element in document.elements if element.metadata.get("part") == "notes"]
    assert [(element.id, element.content) for element in notes] == [
        ("ppt:slide:1:notes", "Mention Q3")
    ]
    assert "Notes:\nMention Q3" in document.content


def test_stream_order_fallback_without_current_user_stream() -> None:
    data = build_ppt_bytes([PptSlide("Only title", "Only body")], include_current_user=False)
    document = _parse(data)

    assert document.content == "Slide 1\nOnly title\nOnly body"
    assert MASTER_PROMPT not in document.content


def test_stale_current_user_pointer_falls_back_to_stream_order() -> None:
    data = build_ppt_bytes([PptSlide("Recovered", "Still readable")], stale_edit_pointer=True)
    document = _parse(data)

    assert document.content == "Slide 1\nRecovered\nStill readable"


def test_text_stored_in_slide_list_and_shape_is_not_duplicated() -> None:
    document = _parse(build_ppt_bytes([PptSlide("Same text", "Same text")]))
    assert document.content == "Slide 1\nSame text"


def test_encrypted_presentation_raises_password_protected() -> None:
    with pytest.raises(PasswordProtectedError):
        _parse(build_ppt_bytes([PptSlide("secret")], encryption="token"))


def test_crypt_session_referenced_by_user_edit_raises_password_protected() -> None:
    with pytest.raises(PasswordProtectedError):
        _parse(build_ppt_bytes([PptSlide("secret")], encryption="session"))


def test_user_edit_tail_without_crypt_session_is_not_treated_as_encrypted() -> None:
    document = _parse(build_ppt_bytes([PptSlide("Open deck")], encryption="tail"))
    assert document.content == "Slide 1\nOpen deck"


def test_encrypted_summary_stream_raises_password_protected() -> None:
    data = build_compound_file(
        {
            "PowerPoint Document": b"\x00" * 16,
            "EncryptedSummary": b"\x00" * 16,
        }
    )
    with pytest.raises(PasswordProtectedError):
        _parse(data)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_deck()[:2048], id="truncated-ole"),
        pytest.param(
            build_compound_file(
                {
                    "PowerPoint Document": b"\x0f\x00\xe8\x03\xff\xff\xff\x7f",
                    "Current User": b"\x00" * 32,
                }
            ),
            id="bad-current-user",
        ),
        pytest.param(
            build_compound_file({"PowerPoint Document": b"\x0f\x00\xe8\x03\xff\xff\xff\x7f"}),
            id="record-overrun",
        ),
    ],
)
def test_corrupt_presentations_raise_parse_error(data: bytes) -> None:
    with pytest.raises(ParseError):
        _parse(data)


def test_ole_file_without_powerpoint_stream_is_unsupported() -> None:
    with pytest.raises(UnsupportedFormatError, match="PowerPoint Document"):
        _parse(build_doc_bytes("Not a deck\r"))


def test_renamed_pptx_is_delegated_to_python_pptx() -> None:
    document = _parse(build_pptx_bytes("Renamed deck"))
    assert document.parser_name == "pptx"
    assert "Renamed deck" in document.content


def test_rtf_payload_named_ppt_is_unsupported() -> None:
    with pytest.raises(UnsupportedFormatError, match="RTF"):
        _parse(b"{\\rtf1 nope}")


def test_empty_input_returns_empty_document() -> None:
    document = _parse(b"")
    assert document.content == ""
    assert document.metadata["slide_count"] == 0


@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [("DECK.PPT", None), ("show.pps", None), (None, "application/vnd.ms-powerpoint")],
)
def test_default_registry_routes_powerpoint_binary_to_presentation_family(
    filename, mime_type
) -> None:
    registry = HarborParserFactory().create_registry()
    assert registry.resolve(filename=filename, mime_type=mime_type).name == "presentation"

    document = registry.parse(
        ParseInput(content=_deck(), filename=filename, content_type=mime_type)
    )
    assert "Quarterly Review" in document.content
