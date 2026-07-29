"""Concurrency tests for shared parser registries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from harborrag_adapters.parsers import HarborParserFactory
from harborrag_adapters.parsers.registry import HarborParserRegistry
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


def _routing_snapshot(parser: HarborParserRegistry) -> dict[str, str]:
    """Record which family each template suffix resolves to, via the public API."""

    return {
        suffix: parser.resolve(f"probe.{suffix}", None).parser_name for suffix, _ in _DOC_TEMPLATES
    }


def test_concurrent_parse_is_deterministic() -> None:
    parser = HarborParserFactory().create_registry()
    inputs = [_make_input(i) for i in range(len(_DOC_TEMPLATES))]
    baseline = [parser.parse(inp).content for inp in inputs]

    tasks = [(i % len(inputs)) for i in range(200)]

    def _run(idx: int) -> tuple[int, str]:
        return idx, parser.parse(inputs[idx]).content

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(_run, tasks))

    assert len(outcomes) == len(tasks)
    for idx, content in outcomes:
        assert content == baseline[idx]


def test_concurrent_parse_leaves_routing_intact() -> None:
    parser = HarborParserFactory().create_registry()
    inputs = [_make_input(i) for i in range(len(_DOC_TEMPLATES))]
    routing_before = _routing_snapshot(parser)
    families_before = sorted(family.parser_name for family in parser.families())

    def _run(idx: int) -> str:
        return parser.parse(inputs[idx]).content

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_run, [(i % len(inputs)) for i in range(200)]))

    assert _routing_snapshot(parser) == routing_before
    assert sorted(family.parser_name for family in parser.families()) == families_before
