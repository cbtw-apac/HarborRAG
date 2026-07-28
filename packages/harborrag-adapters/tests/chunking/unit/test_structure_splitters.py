from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from harborrag_adapters.chunking.htmlsplitter import HtmlStructureSplitter
from harborrag_adapters.chunking.jsonsplitter import JsonStructureSplitter
from harborrag_adapters.chunking.markdownsplitter import MarkdownStructureSplitter
from harborrag_core.contracts.chunking import (
    JsonStructureSplitRequest,
    SourceSpan,
    SplitBoundaryKind,
    StructureSplitRequest,
)


class CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)


class FakeHeaderSplitter:
    def __init__(self, **options: Any) -> None:
        self.options = options

    def split_text(self, content: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(page_content="Intro", metadata={"h1": "Guide"}),
            SimpleNamespace(
                page_content="Details",
                metadata={"h1": "Guide", "h2": "Setup"},
            ),
        ]


def test_markdown_adapter_returns_only_harborrag_splits_with_heading_paths() -> None:
    splitter = MarkdownStructureSplitter(
        CharacterCounter(),
        splitter_factory=FakeHeaderSplitter,
    )

    results = splitter.split(
        StructureSplitRequest(
            content="# Guide\nIntro",
            source_span=SourceSpan(element_ids=("markdown:0",)),
        )
    )

    assert [result.content for result in results] == ["Intro", "Details"]
    assert [result.structural_path for result in results] == [
        ("Guide",),
        ("Guide", "Setup"),
    ]
    assert all(result.boundary_kind == SplitBoundaryKind.SECTION for result in results)
    assert results[0].source_span is not None
    assert results[0].source_span.element_ids == ("markdown:0",)


def test_html_adapter_returns_harborrag_splits_without_framework_documents() -> None:
    splitter = HtmlStructureSplitter(
        CharacterCounter(),
        splitter_factory=FakeHeaderSplitter,
    )

    results = splitter.split(StructureSplitRequest(content="<h1>Guide</h1>"))

    assert [result.content for result in results] == ["Intro", "Details"]
    assert all(type(result).__name__ == "TextSplit" for result in results)


def test_structure_adapters_drop_blank_provider_documents() -> None:
    class BlankHeaderSplitter(FakeHeaderSplitter):
        def split_text(self, content: str) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(page_content=" \n", metadata={"h1": "Blank"}),
                SimpleNamespace(page_content="Content", metadata={"h1": "Kept"}),
            ]

    splitter = MarkdownStructureSplitter(
        CharacterCounter(),
        splitter_factory=BlankHeaderSplitter,
    )

    results = splitter.split(StructureSplitRequest(content="# Kept\nContent"))

    assert [result.content for result in results] == ["Content"]


def test_json_adapter_preserves_paths_and_does_not_mutate_input() -> None:
    value = {"projects": [{"id": 1}, {"id": 2}]}

    class FakeJsonSplitter:
        def __init__(self, **options: Any) -> None:
            self.options = options

        def split_json(
            self,
            data: dict[str, Any],
            *,
            convert_lists: bool,
        ) -> list[dict[str, Any]]:
            assert convert_lists
            data["mutated"] = True
            return [
                {"projects": {"0": {"id": 1}}},
                {"projects": {"1": {"id": 2}}},
            ]

    splitter = JsonStructureSplitter(
        CharacterCounter(),
        splitter_factory=FakeJsonSplitter,
    )

    results = splitter.split(JsonStructureSplitRequest(value=value, maximum_characters=20))

    assert value == {"projects": [{"id": 1}, {"id": 2}]}
    assert [result.structural_path for result in results] == [
        ("projects", "0", "id"),
        ("projects", "1", "id"),
    ]
    assert [result.content for result in results] == [
        '{"projects":{"0":{"id":1}}}',
        '{"projects":{"1":{"id":2}}}',
    ]


def test_json_adapter_supports_root_arrays_without_leaking_wrapper_path() -> None:
    value = [{"id": 1}, {"id": 2}]

    class FakeJsonSplitter:
        def __init__(self, **options: Any) -> None:
            self.options = options
            self.calls = 0

        def split_json(
            self,
            data: dict[str, Any],
            *,
            convert_lists: bool,
        ) -> list[dict[str, Any]]:
            assert convert_lists
            expected = {str(self.calls): value[self.calls]}
            assert data == expected
            self.calls += 1
            return [data]

    splitter = JsonStructureSplitter(
        CharacterCounter(),
        splitter_factory=FakeJsonSplitter,
    )

    results = splitter.split(JsonStructureSplitRequest(value=value))

    assert value == [{"id": 1}, {"id": 2}]
    assert [result.structural_path for result in results] == [
        ("0", "id"),
        ("1", "id"),
    ]
    assert [result.content for result in results] == [
        '{"0":{"id":1}}',
        '{"1":{"id":2}}',
    ]
