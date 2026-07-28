"""Parser throughput tests using many tiny deterministic documents."""

from __future__ import annotations

import pytest

from harborrag_adapters.parsers import HarborParserFactory
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.slow, pytest.mark.blackbox, pytest.mark.timeout(30)]


_DOC_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("md", "# Heading {i}\n\nParagraph body for document {i}."),
    ("txt", "Plain text body for document {i}."),
    ("csv", "col_a,col_b,col_c\n{i},alpha,beta\n{i}0,gamma,delta"),
    ("json", '{{"id": {i}, "name": "doc-{i}", "tags": ["a", "b"]}}'),
    ("html", "<html><body><h1>Doc {i}</h1><p>Body {i}</p></body></html>"),
)


def _make_input(index: int) -> ParseInput:
    suffix, template = _DOC_TEMPLATES[index % len(_DOC_TEMPLATES)]
    return ParseInput(
        content=template.format(i=index),
        filename=f"doc{index}.{suffix}",
    )


def test_parse_many_handles_bulk_inputs() -> None:
    parser = HarborParserFactory().create_registry()
    inputs = [_make_input(i) for i in range(2000)]

    results = parser.parse_many(inputs, on_error="skip")

    assert len(results) == len(inputs)
    assert all(document.content for document in results)


def test_parse_many_isolates_a_corrupt_item_at_scale() -> None:
    parser = HarborParserFactory().create_registry()
    good = [_make_input(i) for i in range(2000)]
    corrupt = ParseInput(content='{"broken": ', filename="corrupt.json")
    mixed = [*good[:1000], corrupt, *good[1000:]]

    results = parser.parse_many(mixed, on_error="skip")

    assert len(results) == len(good)
