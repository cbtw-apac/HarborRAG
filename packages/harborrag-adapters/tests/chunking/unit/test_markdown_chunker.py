"""Whole-file Markdown chunking and the tiktoken counter."""

from __future__ import annotations

import pytest

from harborrag_adapters.chunking import (
    ChunkRole,
    MarkdownChunker,
    TiktokenTokenCounter,
    build_text_refiner,
)

PAPER = """# Paper Title

Jane Doe, John Roe
jane@example.org

## 1 Introduction

Intro words one two three four five six seven eight nine ten eleven twelve.
More intro text here with words thirteen fourteen fifteen sixteen.

## 2 Method

### 2.1 Data

Data paragraph one two three four five six seven eight nine ten.

### 2.2 Model

Model paragraph one two three four five six seven eight nine ten.

## Acknowledgements

We thank everyone.

## References

[1] A. Author. A paper. 2020.
"""


def _chunker(words) -> MarkdownChunker:
    return MarkdownChunker(
        words,
        maximum_tokens=30,
        minimum_tokens=5,
        refiner=build_text_refiner(words, parser_loader=lambda language: None),
    )


def test_chunks_follow_sections_and_tag_roles(words) -> None:
    chunks = _chunker(words).chunk(PAPER)

    assert [chunk.role for chunk in chunks] == [
        ChunkRole.FRONT_MATTER,
        ChunkRole.BODY,
        ChunkRole.BODY,
        ChunkRole.BODY,
        ChunkRole.BODY,
        ChunkRole.BACK_MATTER,
        ChunkRole.REFERENCES,
    ]
    # A bare parent heading leads into its first child and is labelled with it.
    assert chunks[3].content.startswith("## 2 Method\n\n### 2.1 Data")
    assert chunks[3].structural_path == ("2 Method", "2.1 Data")
    assert chunks[4].context == "Paper Title › 2 Method › 2.2 Model"


def test_content_is_the_source_range_and_embed_text_fits(words) -> None:
    chunks = _chunker(words).chunk(PAPER)

    position = 0
    for chunk in chunks:
        span = chunk.source_span
        assert PAPER[span.start_offset : span.end_offset] == chunk.content
        assert span.start_offset == position
        position = span.end_offset
        assert chunk.embed_text.startswith(chunk.context)
        assert chunk.embed_text.endswith(chunk.content.strip())
        assert chunk.embed_token_count <= 30
    assert position == len(PAPER)


def test_title_override_opens_every_context(words) -> None:
    chunks = _chunker(words).chunk(PAPER, title="Override")

    assert all(chunk.context.startswith("Override") for chunk in chunks)


def test_fingerprint_wraps_the_refiner(words) -> None:
    assert _chunker(words).fingerprint().startswith("markdown-sections:3(routing:1(")


@pytest.mark.parametrize(
    "kwargs",
    [{"maximum_tokens": 0}, {"overlap_tokens": 30}, {"minimum_tokens": 31}],
)
def test_rejects_invalid_limits(words, kwargs) -> None:
    options = {"maximum_tokens": 30, **kwargs}
    with pytest.raises(ValueError):
        MarkdownChunker(
            words, refiner=build_text_refiner(words, parser_loader=lambda _: None), **options
        )


def test_tiktoken_counter_counts_special_token_text_as_content() -> None:
    pytest.importorskip("tiktoken")
    counter = TiktokenTokenCounter()

    try:
        count = counter.count("before <|endoftext|> after")
    except Exception as exc:  # encoding files are fetched once; offline runs skip
        pytest.skip(f"tiktoken encoding unavailable: {exc}")

    assert count > 3
    assert counter.count("") == 0
    assert counter.version == "cl100k_base"


def test_a_long_context_is_shortened_so_embed_text_never_exceeds_the_maximum(words) -> None:
    title = "A very long document title that goes on and on for many words"
    text = (
        f"# {title}\n\n"
        "## Part one about deployment with a long heading\n\n"
        "### Nested section on rollback procedures in depth\n\n"
        + " ".join(f"Sentence {k} covers a rollback step." for k in range(12))
        + "\n"
    )
    chunker = MarkdownChunker(
        words,
        maximum_tokens=20,
        minimum_tokens=2,
        refiner=build_text_refiner(words, parser_loader=lambda language: None),
    )

    chunks = chunker.chunk(text)

    assert chunks
    for chunk in chunks:
        assert chunk.embed_token_count <= 20
        assert chunk.token_count <= 20
        assert text[chunk.source_span.start_offset : chunk.source_span.end_offset] == chunk.content
    body = [chunk for chunk in chunks if "Sentence" in chunk.content]
    assert body
    # The most specific heading survives; the title and outer headings go first.
    assert all(
        chunk.context.endswith("Nested section on rollback procedures in depth") for chunk in body
    )
    assert all(title not in chunk.context for chunk in body)
