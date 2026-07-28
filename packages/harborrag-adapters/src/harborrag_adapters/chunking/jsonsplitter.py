from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any, cast

from harborrag_core.contracts.chunking import (
    JsonStructureSplitRequest,
    SplitBoundaryKind,
    TextSplit,
    TokenCounter,
)

from .base import HarborBaseChunk
from .provenance import structural_span


class JsonStructureSplitter(HarborBaseChunk[JsonStructureSplitRequest]):
    """Split JSON objects while retaining deterministic structural paths."""

    chunk_name = "json"
    required_dependency = "langchain_text_splitters"
    request_type = JsonStructureSplitRequest

    def __init__(
        self,
        token_counter: TokenCounter,
        *,
        splitter_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(token_counter)
        self._splitter_factory = splitter_factory

    def split(self, request: JsonStructureSplitRequest) -> tuple[TextSplit, ...]:
        """Convert a JSON value into ordered HarborRAG split results."""

        if not request.value:
            return ()
        factory = self._splitter_factory or self._load_splitter_factory()
        splitter = factory(
            max_chunk_size=request.maximum_characters,
            min_chunk_size=request.minimum_characters,
        )
        root_array = isinstance(request.value, Sequence) and not isinstance(
            request.value, (str, bytes, bytearray)
        )
        payloads = (
            [{str(index): deepcopy(item)} for index, item in enumerate(request.value)]
            if root_array
            else [deepcopy(dict(request.value))]
        )
        fragments = [
            fragment
            for payload in payloads
            for fragment in splitter.split_json(
                payload,
                convert_lists=request.convert_lists,
            )
        ]
        results: list[TextSplit] = []
        for fragment in fragments:
            content = json.dumps(
                fragment,
                ensure_ascii=request.ensure_ascii,
                separators=(",", ":"),
            )
            count = self._token_counter.count(content)
            if count < 1:
                continue
            results.append(
                TextSplit(
                    content=content,
                    token_count=count,
                    source_span=structural_span(request.source_span),
                    boundary_kind=SplitBoundaryKind.JSON_PATH,
                    structural_path=self._structural_path(fragment),
                )
            )
        return tuple(results)

    @staticmethod
    def _structural_path(value: object) -> tuple[str, ...]:
        path: list[str] = []
        current = value
        while isinstance(current, Mapping) and len(current) == 1:
            key, current = next(iter(current.items()))
            path.append(str(key))
        return tuple(path)

    @staticmethod
    def _load_splitter_factory() -> Callable[..., Any]:
        try:
            from langchain_text_splitters import RecursiveJsonSplitter
        except ImportError as exc:
            raise ImportError(
                "JSON structure splitting requires the harborrag-adapters[chunking] extra"
            ) from exc
        return cast(Callable[..., Any], RecursiveJsonSplitter)
