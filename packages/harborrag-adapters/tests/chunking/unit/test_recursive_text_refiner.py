from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest
from harborrag_adapters.chunking.recursive import RecursiveTextRefiner
from harborrag_core.contracts.chunking import SourceSpan, TextRefinementRequest


class CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)


class FakeRecursiveSplitter:
    options: dict[str, Any]

    def __init__(self, **options: Any) -> None:
        self.options = options

    def create_documents(self, values: list[str]) -> list[SimpleNamespace]:
        value = values[0]
        size = int(self.options["chunk_size"])
        overlap = int(self.options["chunk_overlap"])
        documents = []
        start = 0
        while start < len(value):
            end = min(start + size, len(value))
            documents.append(
                SimpleNamespace(
                    page_content=value[start:end],
                    metadata={"start_index": start},
                )
            )
            if end == len(value):
                break
            start = end - overlap
        return documents


def test_recursive_refiner_uses_maximum_as_hard_size_and_tracks_spans() -> None:
    refiner = RecursiveTextRefiner(
        CharacterCounter(),
        splitter_factory=FakeRecursiveSplitter,
    )
    request = TextRefinementRequest(
        content="abcdefghij",
        maximum_tokens=4,
        overlap_tokens=1,
        source_span=SourceSpan(
            start_offset=10,
            end_offset=20,
            start_line=7,
            end_line=7,
            element_ids=("paragraph-1",),
        ),
    )

    splits = refiner.split(request)

    assert [split.content for split in splits] == ["abcd", "defg", "ghij"]
    assert all(split.token_count <= 4 for split in splits)
    assert all(split.forced_split for split in splits)
    assert [split.source_span.start_offset for split in splits if split.source_span] == [
        10,
        13,
        16,
    ]


def test_recursive_refiner_returns_in_limit_content_without_loading_provider() -> None:
    def fail_factory(**options: Any) -> Any:
        raise AssertionError(f"provider should not be loaded: {options}")

    refiner = RecursiveTextRefiner(
        CharacterCounter(),
        splitter_factory=fail_factory,
    )

    splits = refiner.split(TextRefinementRequest(content="short", maximum_tokens=10))

    assert len(splits) == 1
    assert splits[0].content == "short"
    assert not splits[0].forced_split


def test_recursive_refiner_drops_blank_content() -> None:
    refiner = RecursiveTextRefiner(CharacterCounter())

    assert refiner.split(TextRefinementRequest(content=" \n", maximum_tokens=10)) == ()


def test_recursive_refiner_reports_the_missing_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "langchain_text_splitters", None)
    refiner = RecursiveTextRefiner(CharacterCounter())

    with pytest.raises(ImportError, match=r"harborrag-adapters\[chunking\]"):
        refiner.split(TextRefinementRequest(content="too long", maximum_tokens=4))


def test_recursive_refiner_rejects_provider_content_loss() -> None:
    class LossySplitter:
        def __init__(self, **options: Any) -> None:
            pass

        def create_documents(self, values: list[str]) -> list[SimpleNamespace]:
            return [SimpleNamespace(page_content="abc", metadata={"start_index": 0})]

    refiner = RecursiveTextRefiner(
        CharacterCounter(),
        splitter_factory=LossySplitter,
    )

    with pytest.raises(ValueError, match="trailing source content"):
        refiner.split(TextRefinementRequest(content="abcdef", maximum_tokens=3))


def test_recursive_refiner_falls_back_when_provider_start_index_is_invalid() -> None:
    class InvalidMetadataSplitter:
        def __init__(self, **options: Any) -> None:
            pass

        def create_documents(self, values: list[str]) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(page_content="abcd", metadata={"start_index": 0}),
                SimpleNamespace(page_content="defg", metadata={"start_index": -1}),
                SimpleNamespace(page_content="ghij", metadata={"start_index": 6}),
            ]

        def split_text(self, value: str) -> list[str]:
            return ["abcd", "defg", "ghij"]

    refiner = RecursiveTextRefiner(
        CharacterCounter(),
        splitter_factory=InvalidMetadataSplitter,
    )

    splits = refiner.split(
        TextRefinementRequest(
            content="abcdefghij",
            maximum_tokens=4,
            overlap_tokens=1,
        )
    )

    assert [split.source_span.start_offset for split in splits if split.source_span] == [
        0,
        3,
        6,
    ]


def test_recursive_refiner_uses_inclusive_end_lines_for_half_open_offsets() -> None:
    refiner = RecursiveTextRefiner(
        CharacterCounter(),
        splitter_factory=FakeRecursiveSplitter,
    )

    splits = refiner.split(
        TextRefinementRequest(
            content="ab\ncd",
            maximum_tokens=3,
            source_span=SourceSpan(
                start_offset=20,
                end_offset=25,
                start_line=4,
                end_line=5,
            ),
        )
    )

    assert [
        (split.source_span.start_line, split.source_span.end_line)
        for split in splits
        if split.source_span
    ] == [(4, 4), (5, 5)]
