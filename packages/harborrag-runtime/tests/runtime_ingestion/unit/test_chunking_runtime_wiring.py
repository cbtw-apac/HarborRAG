"""Runtime wires the structure-aware refiner and the configured token counter."""

from __future__ import annotations

import pytest

from harborrag_adapters.chunking import RoutingTextRefiner
from harborrag_runtime.ingestion.runtime_builder import build_default_chunker
from harborrag_runtime.tokenization import ApproximateTokenCounter, chunking_token_counter


def test_default_chunker_refines_through_the_routing_refiner() -> None:
    chunker = build_default_chunker()

    refiner = chunker._candidates._transforms.refiner._refiner

    assert isinstance(refiner, RoutingTextRefiner)


def test_chunking_token_counter_selection() -> None:
    assert isinstance(chunking_token_counter("approximate"), ApproximateTokenCounter)
    with pytest.raises(ValueError, match="unknown chunking tokenizer"):
        chunking_token_counter("sentencepiece")


def test_tiktoken_counter_is_selected_by_name() -> None:
    pytest.importorskip("tiktoken")

    counter = chunking_token_counter("tiktoken:cl100k_base")

    assert counter.name == "tiktoken"
    assert counter.version == "cl100k_base"
