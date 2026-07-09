"""Performance, scale, and resource-safety tests for the adapters package.

These are deliberately deterministic and CI-cheap: modest input counts and
sizes stand in for production scale, and every expensive dependency (Docling,
LiteParse, network I/O) is faked so the *mechanisms* that matter at scale --
per-item failure isolation, input-size guards, model memoization, thread-safe
read-only parsing, and streaming caps -- are exercised without real load.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from harborrag_core.domain.parser import ParseInput

from harborrag_adapters.connectors.http_utils import (
    ResponseTooLargeError,
    read_capped_content,
)
from harborrag_adapters.parsers.engine import HarborParser
from harborrag_adapters.parsers.exceptions import ParseError
from harborrag_adapters.parsers.pdf_engine.base import PdfBackend, PdfParseResult
from harborrag_adapters.parsers.pdf_engine.docling import (
    DoclingBackend,
    DoclingBackendOptions,
)
from harborrag_adapters.parsers.pdf_engine.liteparse import (
    LiteParseBackend,
    LiteParseBackendOptions,
)
from harborrag_adapters.parsers.pdf_engine.parser import PdfParser
from harborrag_adapters.parsers.utils import DEFAULT_MAX_INPUT_BYTES, guard_input_size

from harbor_test_builders import FakeResponse


pytestmark = pytest.mark.timeout(30)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# (suffix, content) templates for cheap, unambiguously-routable text documents.
_DOC_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("md", "# Heading {i}\n\nParagraph body for document {i}."),
    ("txt", "Plain text body for document {i}."),
    ("csv", "col_a,col_b,col_c\n{i},alpha,beta\n{i}0,gamma,delta"),
    ("json", '{{"id": {i}, "name": "doc-{i}", "tags": ["a", "b"]}}'),
    ("html", "<html><body><h1>Doc {i}</h1><p>Body {i}</p></body></html>"),
)


def _make_input(index: int) -> ParseInput:
    """Build one tiny, suffix-routed ParseInput cycling through text formats."""

    suffix, template = _DOC_TEMPLATES[index % len(_DOC_TEMPLATES)]
    # Only a filename is set (no content_type) so routing is decided purely by
    # suffix and cannot hit the conflicting-route branch.
    return ParseInput(
        content=template.format(i=index),
        filename=f"doc{index}.{suffix}",
    )


# ---------------------------------------------------------------------------
# 1) Bulk throughput / stability
# ---------------------------------------------------------------------------


def test_parse_many_bulk_throughput_is_stable_and_fast() -> None:
    """~2000 mixed small documents all parse quickly under parse_many."""

    parser = HarborParser()
    inputs = [_make_input(i) for i in range(2000)]

    start = time.perf_counter()
    results = parser.parse_many(inputs, on_error="skip")
    elapsed = time.perf_counter() - start

    assert len(results) == len(inputs)
    assert all(document.content for document in results)
    # Comfortably inside the 30s module timeout; guards against accidental
    # per-item work (model builds, re-registration) creeping into the hot path.
    assert elapsed < 15.0


def test_parse_many_isolates_a_corrupt_item_at_scale() -> None:
    """A single malformed document is skipped; every good one still returns."""

    parser = HarborParser()
    good = [_make_input(i) for i in range(2000)]
    # Invalid JSON with a .json suffix routes to JsonParser and raises ParseError,
    # which parse_many(on_error="skip") must log-and-drop rather than abort on.
    corrupt = ParseInput(content='{"broken": ', filename="corrupt.json")
    mixed = [*good[:1000], corrupt, *good[1000:]]

    results = parser.parse_many(mixed, on_error="skip")

    assert len(results) == len(good)  # exactly the good ones survived


# ---------------------------------------------------------------------------
# 2) Large single input bounded by the size guard
# ---------------------------------------------------------------------------


def test_multi_megabyte_text_inputs_parse() -> None:
    """A few-MB CSV and a large JSON payload parse without special handling."""

    parser = HarborParser()

    csv_row = "1234567,alpha,beta,gamma,delta\n"
    csv_body = "a,b,c,d,e\n" + csv_row * 60_000  # ~1.8 MB
    assert len(csv_body.encode("utf-8")) > 1_500_000
    csv_doc = parser.parse(ParseInput(content=csv_body, filename="big.csv"))
    assert csv_doc.content
    assert csv_doc.parser_name == "csv"

    json_body = "[" + ",".join(f'"item-{i:05d}"' for i in range(80_000)) + "]"
    assert len(json_body.encode("utf-8")) > 1_000_000
    json_doc = parser.parse(ParseInput(content=json_body, filename="big.json"))
    assert json_doc.content
    assert json_doc.parser_name == "json"


def test_guard_input_size_mechanism_without_large_allocation() -> None:
    """guard_input_size rejects just-over inputs; the default cap is large."""

    # Prove the mechanism with a modest buffer and a small cap instead of
    # allocating anywhere near DEFAULT_MAX_INPUT_BYTES.
    data = bytes(bytearray(1024))
    assert guard_input_size(data, max_bytes=len(data)) is data  # exactly at cap
    assert guard_input_size(data, max_bytes=len(data) + 1) is data  # under cap

    with pytest.raises(ParseError) as excinfo:
        guard_input_size(data, max_bytes=len(data) - 1)  # one byte over
    message = str(excinfo.value)
    assert str(len(data)) in message and str(len(data) - 1) in message

    # The production default is genuinely large (hundreds of MB), so real
    # documents are never clipped by it in practice.
    assert DEFAULT_MAX_INPUT_BYTES == 512 * 1024 * 1024
    assert DEFAULT_MAX_INPUT_BYTES >= 100 * 1024 * 1024


# ---------------------------------------------------------------------------
# 3) Model memoization (scale-critical)
# ---------------------------------------------------------------------------

# Module-level counter incremented only when a fake backend builds its
# (notionally expensive) model, so we can assert "build once, reuse many".
_MODEL_BUILDS = 0


class _CountingPdfBackend(PdfBackend):
    """Fake PDF backend whose model is built lazily and memoized per instance."""

    name = "counting-fake"

    def __init__(self) -> None:
        self._cached_model = None

    def _model(self):
        if self._cached_model is None:
            global _MODEL_BUILDS
            _MODEL_BUILDS += 1
            self._cached_model = object()
        return self._cached_model

    def parse(self, input: ParseInput) -> PdfParseResult:
        model = self._model()
        assert model is self._cached_model
        return PdfParseResult(
            content="fake extracted pdf content that is long enough",
            engine=self.name,
        )


class _FakeDoclingDocument:
    """Minimal Docling-document stand-in exposing the markdown export hook."""

    def export_to_markdown(self, **_kwargs) -> str:
        return "# Injected\n\nFake docling content long enough to be accepted."


class _FakeDoclingConverter:
    """Fake DocumentConverter recording how many times convert() ran."""

    def __init__(self) -> None:
        self.convert_calls = 0

    def convert(self, path, **_kwargs):
        self.convert_calls += 1
        return _FakeDoclingDocument()


def test_pdf_parser_reuses_one_backend_instance_across_many_parses() -> None:
    """A single PdfParser reuses its backend, building the model exactly once."""

    global _MODEL_BUILDS
    _MODEL_BUILDS = 0

    parser = PdfParser(backends=[_CountingPdfBackend()], min_content_chars=5)
    for i in range(300):
        document = parser.parse(
            ParseInput(content=b"%PDF-1.4 fake", filename=f"scan{i}.pdf")
        )
        assert document.parser_name == "pdf"

    # The expensive resource is memoized on the long-lived backend, so 300
    # parses cost exactly one build.
    assert _MODEL_BUILDS == 1


def test_backend_cache_fields_start_none() -> None:
    """Fresh backends declare an unset memoization slot (no eager model load)."""

    assert DoclingBackend()._cached_converter is None
    assert LiteParseBackend()._cached_parser is None


def test_docling_injected_converter_is_reused_by_identity() -> None:
    """An injected converter short-circuits caching and is reused every parse."""

    converter = _FakeDoclingConverter()
    backend = DoclingBackend(DoclingBackendOptions(converter=converter))

    # The private builder returns the *same* object on repeated calls...
    assert backend._converter() is converter
    assert backend._converter() is converter
    # ...and the injection path never populates (nor needs) the cache slot.
    assert backend._cached_converter is None

    doc1 = backend.parse(ParseInput(content=b"%PDF-1.4", filename="a.pdf"))
    doc2 = backend.parse(ParseInput(content=b"%PDF-1.4", filename="b.pdf"))
    assert doc1.content and doc2.content
    assert converter.convert_calls == 2  # driven twice, one shared converter


def test_docling_cached_converter_branch_returns_same_identity() -> None:
    """Once populated, the cache slot yields one object without rebuilding."""

    backend = DoclingBackend()  # no injected converter
    sentinel = object()
    backend._cached_converter = sentinel  # simulate a completed one-time build

    # Both calls return the cached instance by identity and never touch the
    # (absent) real Docling import path.
    assert backend._converter() is sentinel
    assert backend._converter() is sentinel


def test_liteparse_injected_parser_is_reused_by_identity() -> None:
    """LiteParse honors an injected parser and leaves its cache slot unset."""

    class _FakeLiteResult:
        text = "fake liteparse content long enough to accept"
        pages = []

    class _FakeLiteParse:
        def __init__(self) -> None:
            self.parse_calls = 0

        def parse(self, _path):
            self.parse_calls += 1
            return _FakeLiteResult()

    fake = _FakeLiteParse()
    backend = LiteParseBackend(LiteParseBackendOptions(parser=fake))

    assert backend._parser() is fake
    assert backend._parser() is fake
    assert backend._cached_parser is None

    backend.parse(ParseInput(content=b"%PDF-1.4", filename="a.pdf"))
    backend.parse(ParseInput(content=b"%PDF-1.4", filename="b.pdf"))
    assert fake.parse_calls == 2


# ---------------------------------------------------------------------------
# 4) Concurrency: read-only parsing is thread-safe and deterministic
# ---------------------------------------------------------------------------


def test_concurrent_parse_is_deterministic_and_route_tables_intact() -> None:
    """Parsing independent inputs from many threads is safe and reproducible."""

    parser = HarborParser()
    # One representative input per supported text format.
    inputs = [_make_input(i) for i in range(len(_DOC_TEMPLATES))]

    # Single-threaded baseline for determinism comparison.
    baseline = [parser.parse(inp).content for inp in inputs]

    # Snapshot route tables to detect any concurrent corruption.
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
        assert content == baseline[idx]  # identical to serial result

    # Read-only parsing must not mutate the shared registry's route indexes.
    assert dict(parser._by_suffix) == suffix_before
    assert dict(parser._by_content_type) == content_type_before
    assert dict(parser._by_name) == name_before


# ---------------------------------------------------------------------------
# 5) Streaming cap efficiency
# ---------------------------------------------------------------------------


class _CountingResponse(FakeResponse):
    """FakeResponse that records how many bytes iter_content actually yielded."""

    def iter_content(self, chunk_size: int = 65536):
        self.consumed = 0
        for chunk in self._chunks:
            self.consumed += len(chunk)
            yield chunk


def test_read_capped_content_aborts_early_without_buffering_everything() -> None:
    """The cap raises after reading ~cap bytes, not the whole body."""

    chunk = b"x" * 1024
    total_chunks = 100  # 100 KiB of body available
    cap = 5_000

    response = _CountingResponse(_chunks=[chunk] * total_chunks)

    with pytest.raises(ResponseTooLargeError):
        read_capped_content(response, cap)

    total_available = len(chunk) * total_chunks
    assert response.consumed < total_available  # did NOT drain the full body
    # At most one extra chunk beyond the cap is read before the abort.
    assert response.consumed <= cap + len(chunk)
    assert response.closed is True  # connection released on abort


def test_read_capped_content_rejects_oversized_content_length_upfront() -> None:
    """A declared-oversized Content-Length is rejected before streaming any body.

    ``ResponseTooLargeError`` subclasses ``ValueError``, so this only holds while
    the header short-circuit raises *outside* the ``int(...)`` try/except; if the
    raise slips back inside it, the exception is swallowed and the check becomes
    dead code. This pins the header pre-check as effective: no body is consumed.
    """

    chunk = b"x" * 1024
    response = _CountingResponse(
        headers={"Content-Length": "999999"},
        _chunks=[chunk] * 50,
    )

    with pytest.raises(ResponseTooLargeError, match="Content-Length"):
        read_capped_content(response, 1_000)

    assert response.closed is True
    # The body stream was never entered: iter_content never ran, so the counter
    # attribute was never even created.
    assert not hasattr(response, "consumed")


def test_read_capped_content_rejects_unparseable_content_length_via_stream() -> None:
    """A non-numeric Content-Length falls through to the incremental cap."""

    chunk = b"x" * 1024
    response = _CountingResponse(
        headers={"Content-Length": "not-a-number"},
        _chunks=[chunk] * 50,
    )

    with pytest.raises(ResponseTooLargeError, match="Downloaded body"):
        read_capped_content(response, 1_000)

    assert response.closed is True
    # Header was unusable, but the streaming cap still bounds memory to ~cap.
    assert response.consumed <= 1_000 + len(chunk)
    assert response.consumed < len(chunk) * 50


def test_read_capped_content_returns_body_within_cap() -> None:
    """A body under the cap streams through and is returned intact."""

    response = _CountingResponse(_chunks=[b"abc", b"def", b"ghi"])
    body = read_capped_content(response, 1_000)
    assert body == b"abcdefghi"
