"""Parse timing by parser family, recorded around the parser ingestion injects.

The decorated parser behaves exactly like the one it wraps. Only the parser family name
and the duration are recorded, never the document or its file name.
"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from harborrag_core.domain.parser import ParsedDocument
    from harborrag_core.domain.raw_document import RawDocument
    from harborrag_engine.ingestion.release.dependencies import DocumentParserPort


class ParseDurationSink(Protocol):
    """Where parse durations are recorded."""

    def record_parse(self, parser: str, duration_seconds: float) -> None:
        """Record how long ``parser`` took to parse one document."""
        ...


class TimedDocumentParser:
    """Document parser that records each successful parse's duration by parser family.

    A failed parse is not timed here, because its stage outcome already counts it.

    Args:
        parser: The parser that does the work.
        sink: Receives the parser family name and the duration.
    """

    def __init__(self, parser: DocumentParserPort, sink: ParseDurationSink) -> None:
        self._parser = parser
        self._sink = sink

    def parse(self, source: RawDocument, /) -> ParsedDocument:
        """Parse ``source`` with the wrapped parser and record how long it took."""
        started = perf_counter()
        document = self._parser.parse(source)
        self._sink.record_parse(document.parser_name, perf_counter() - started)
        return document
