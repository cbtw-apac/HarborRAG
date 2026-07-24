"""Concurrency tests for shared parser registries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from harborrag_adapters.parsers.engine import HarborParser
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.slow, pytest.mark.graybox, pytest.mark.timeout(30)]


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


def test_concurrent_parse_is_deterministic_and_route_tables_intact() -> None:
    parser = HarborParser()
    inputs = [_make_input(i) for i in range(len(_DOC_TEMPLATES))]
    baseline = [parser.parse(inp).content for inp in inputs]

    suffix_before = dict(parser._by_suffix)
    content_type_before = dict(parser._by_content_type)
    name_before = dict(parser._by_name)

    tasks = [(i % len(inputs)) for i in range(200)]

    def _run(idx: int) -> tuple[int, str]:
        return idx, parser.parse(inputs[idx]).content

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(_run, tasks))

    assert len(outcomes) == len(tasks)
    for idx, content in outcomes:
        assert content == baseline[idx]

    assert dict(parser._by_suffix) == suffix_before
    assert dict(parser._by_content_type) == content_type_before
    assert dict(parser._by_name) == name_before
