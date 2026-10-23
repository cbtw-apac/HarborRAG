"""Crafted legacy `.ppt` slide lists cannot amplify parser output."""

from __future__ import annotations

import struct

import pytest

from harborrag_adapters.parsers.common.validation import ParseResourceBudget
from harborrag_adapters.parsers.errors import ParseError
from harborrag_adapters.parsers.presentation.engines.msppt import engine as ppt_engine
from harborrag_adapters.parsers.presentation.engines.msppt import records as rec
from harborrag_adapters.parsers.presentation.engines.msppt.engine import (
    MsPowerPointBinaryPresentationEngine as Engine,
)

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def _record(record_type: int, payload: bytes, *, version: int = 0, instance: int = 0) -> bytes:
    return struct.pack("<HHI", (instance << 4) | version, record_type, len(payload)) + payload


def _container(record_type: int, *children: bytes, instance: int = 0) -> bytes:
    return _record(record_type, b"".join(children), version=0xF, instance=instance)


def _persist_atom(persist_id: int, slide_id: int) -> bytes:
    return _record(rec.RT_SLIDE_PERSIST_ATOM, struct.pack("<IIiI", persist_id, 0, 0, slide_id))


def _slide(text: str, notes_id: int = 0) -> bytes:
    atom = _record(rec.RT_SLIDE_ATOM, struct.pack("<I8sII", 0, b"\0" * 8, 0, notes_id), version=2)
    return _container(rec.RT_SLIDE, atom, _record(rec.RT_TEXT_BYTES_ATOM, text.encode("latin-1")))


def _presentation(
    containers: list[bytes],
    persist: dict[int, int],
    slide_atoms: list[bytes],
    notes_atoms: tuple[bytes, ...] = (),
) -> tuple[bytes, bytes]:
    """Lay out ``containers``; ``persist`` maps each persist id to a container index."""
    stream = bytearray()
    offsets = []
    for container in containers:
        offsets.append(len(stream))
        stream += container
    document_id = max(persist) + 1
    offsets.append(len(stream))
    stream += _container(
        rec.RT_DOCUMENT,
        _container(rec.RT_SLIDE_LIST_WITH_TEXT, *slide_atoms, instance=rec.SLIDE_LIST_SLIDES),
        _container(rec.RT_SLIDE_LIST_WITH_TEXT, *notes_atoms, instance=rec.SLIDE_LIST_NOTES),
    )
    mapping = {**{pid: offsets[index] for pid, index in persist.items()}, document_id: offsets[-1]}
    directory_offset = len(stream)
    stream += _record(
        rec.RT_PERSIST_DIRECTORY_ATOM,
        b"".join(struct.pack("<II", pid | (1 << 20), offset) for pid, offset in mapping.items()),
    )
    edit_offset = len(stream)
    edit = struct.pack("<II", 0, 0) + struct.pack("<III", 0, directory_offset, document_id)
    stream += _record(rec.RT_USER_EDIT_ATOM, edit + b"\0" * 8)
    current_user = _record(
        rec.RT_CURRENT_USER_ATOM, struct.pack("<III", 20, rec.HEADER_TOKEN_PLAIN, edit_offset)
    )
    return bytes(stream), current_user


def test_many_slide_list_entries_for_one_slide_decode_it_once():
    data, current_user = _presentation(
        [_slide("A" * 10_000)],
        {1: 0},
        [_persist_atom(1, 256 + index) for index in range(2_000)],
    )

    slides = Engine._slides_from_persist_directory(data, current_user)

    assert sum(len(slide.text) for slide in slides) == 10_000


def test_persist_ids_aliasing_one_container_decode_it_once():
    data, current_user = _presentation(
        [_slide("Only slide")],
        {1: 0, 2: 0, 3: 0},
        [_persist_atom(1, 256), _persist_atom(2, 257), _persist_atom(3, 258)],
    )

    slides = Engine._slides_from_persist_directory(data, current_user)

    assert [slide.text for slide in slides] == ["Only slide"]


def test_slides_sharing_one_notes_page_carry_its_text_once():
    notes = _container(rec.RT_NOTES, _record(rec.RT_TEXT_BYTES_ATOM, b"N" * 500))
    data, current_user = _presentation(
        [_slide("first", notes_id=512), _slide("second", notes_id=512), notes],
        {1: 0, 2: 1, 3: 2},
        [_persist_atom(1, 256), _persist_atom(2, 257)],
        (_persist_atom(3, 512),),
    )

    slides = Engine._slides_from_persist_directory(data, current_user)

    assert [slide.text for slide in slides] == ["first", "second"]
    assert sum(len(slide.notes) for slide in slides) == 500


def test_the_output_limit_trips_while_slides_are_extracted(monkeypatch):
    data, current_user = _presentation(
        [_slide(f"{index}" * 1_000) for index in range(10)],
        {index + 1: index for index in range(10)},
        [_persist_atom(index + 1, 256 + index) for index in range(10)],
    )
    decoded: list[int] = []
    collect_text = rec.collect_text

    def counting_collect_text(data, container, depth=0):
        decoded.append(container.start)
        return collect_text(data, container, depth)

    monkeypatch.setattr(ppt_engine.rec, "collect_text", counting_collect_text)

    with pytest.raises(ParseError, match="character limit"):
        Engine._slides_from_persist_directory(
            data, current_user, ParseResourceBudget(max_output_characters=2_500)
        )
    assert len(decoded) == 3


def test_an_oversized_presentation_is_rejected_without_a_stream_order_reparse(monkeypatch):
    pytest.importorskip("olefile", reason="legacy Office parser tests require the parsers extra")
    from legacy_office_builders import PptSlide, build_ppt_bytes

    from harborrag_core.domain.parser import ParseInput

    # Slide text alone fits; the notes push the persist-directory output past the limit,
    # while a notes-less stream-order re-parse would have fit and been accepted.
    deck = build_ppt_bytes([PptSlide("Title", "B" * 200, "N" * 2_000)])
    monkeypatch.setattr(
        ppt_engine, "ParseResourceBudget", lambda: ParseResourceBudget(max_output_characters=1_000)
    )

    def no_fallback(*_args, **_kwargs):
        raise AssertionError("an output-limit failure must not fall back to stream order")

    monkeypatch.setattr(Engine, "_slides_from_stream_order", staticmethod(no_fallback))

    with pytest.raises(ParseError, match="character limit"):
        Engine().parse(ParseInput(content=deck, filename="deck.ppt"))
