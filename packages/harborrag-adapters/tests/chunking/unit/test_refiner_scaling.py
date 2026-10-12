"""Refinement must stay roughly linear on large and hostile units.

Chunking runs inside the worker activity rather than the killable parse
process, so a superlinear refiner holds an activity slot until it times out.
Before incremental merge counts and a galloping window, every merge step
recounted the whole growing piece: 300 KB of decorator lines took ~4.7 s.
"""

from __future__ import annotations

import math
import time

import pytest

from harborrag_adapters.chunking import build_text_refiner
from harborrag_core.contracts.chunking import SplitBoundaryKind, TextRefinementRequest

SIZE = 300_000


class ApproximateCounter:
    """The runtime's default estimate: four ASCII characters per token."""

    def count(self, text: str) -> int:
        ascii_characters = sum(ord(character) < 128 for character in text)
        return math.ceil(ascii_characters / 4) + len(text) - ascii_characters


HOSTILE_UNITS = {
    "unpunctuated paragraph": (
        " ".join(f"word{index}" for index in range(SIZE // 6)),
        SplitBoundaryKind.PARAGRAPH,
        None,
    ),
    "no whitespace": ("x" * SIZE, SplitBoundaryKind.PARAGRAPH, None),
    "log lines": (
        "".join(f"2026-10-12 INFO event {index}\n" for index in range(SIZE // 30)),
        SplitBoundaryKind.LINE,
        None,
    ),
    "decorator run": (
        "@x\n" * (SIZE // 3) + "def f():\n    pass\n",
        SplitBoundaryKind.CODE_BLOCK,
        "python",
    ),
}


@pytest.mark.parametrize("unit", HOSTILE_UNITS.values(), ids=HOSTILE_UNITS.keys())
def test_large_units_refine_quickly(unit: tuple[str, SplitBoundaryKind, str | None]) -> None:
    content, kind, language = unit
    request = TextRefinementRequest(
        content=content,
        maximum_tokens=1100,
        overlap_tokens=80 if kind == SplitBoundaryKind.PARAGRAPH else 0,
        boundary_kind=kind,
        language=language,
        preserve_whitespace=kind != SplitBoundaryKind.PARAGRAPH,
    )
    refiner = build_text_refiner(ApproximateCounter())

    started = time.perf_counter()
    splits = refiner.split(request)
    elapsed = time.perf_counter() - started

    assert all(split.token_count <= 1100 for split in splits)
    # ~0.1-0.9 s measured (the old merge took ~4.7 s on the decorator run).
    assert elapsed < 4.0, f"refining took {elapsed:.2f}s"


def test_prose_overlap_survives_packing_to_the_limit() -> None:
    content = " ".join(f"word{index}" for index in range(5_000))
    request = TextRefinementRequest(
        content=content,
        maximum_tokens=200,
        overlap_tokens=20,
        preserve_whitespace=False,
    )

    splits = build_text_refiner(ApproximateCounter()).split(request)

    pairs = list(zip(splits, splits[1:], strict=False))
    assert pairs
    assert all(
        later.source_span.start_offset < earlier.source_span.end_offset
        for earlier, later in pairs
        if earlier.source_span and later.source_span
    )
    assert all(split.token_count <= 200 for split in splits)
