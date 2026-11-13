"""Parser and chunker selection for one run's pinned settings."""

from __future__ import annotations

from collections.abc import Callable

from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_engine.ingestion import BaseChunker
from harborrag_engine.ingestion.chunking import ChunkingConfig
from harborrag_engine.ingestion.chunking.config import cap_chunking_config
from harborrag_runtime.config.chunking import ChunkingSettings
from harborrag_runtime.config.parsers import ParserCatalog
from harborrag_runtime.ingestion_contracts import RunSettings

from .dependencies import DocumentParserPort


class ParserUnavailableError(HarborConfigurationError):
    """The PDF parser a run pins cannot be built on this worker."""


class RunTools:
    """Boot parser and chunker, plus per-run variants built once per distinct override."""

    def __init__(
        self,
        *,
        parser: DocumentParserPort,
        chunker: BaseChunker,
        chunking: ChunkingSettings,
        parsers: ParserCatalog,
        build_chunker: Callable[[ChunkingConfig], BaseChunker],
    ) -> None:
        self._parser = parser
        self._chunker = chunker
        self._chunking = chunking
        self._parsers = parsers
        self._build_chunker = build_chunker
        self._pdf_parsers: dict[str, DocumentParserPort] = {}
        self._chunkers: dict[int, BaseChunker] = {}

    def parser_for(self, run: RunSettings | None) -> DocumentParserPort:
        """Return the parser a run parses with.

        Args:
            run: Pinned settings, or ``None`` for the boot parser.

        Returns:
            The boot parser, or a registry whose PDF family is the pinned backend.

        Raises:
            ParserUnavailableError: ``parsing.pdf_parser`` names a backend with no
                definition in the parser catalog, or its library is not installed.
        """
        backend = run.pdf_parser if run is not None else None
        if backend is None or backend == self._parsers.enabled_pdf_backend():
            return self._parser
        cached = self._pdf_parsers.get(backend)
        if cached is not None:
            return cached
        definition = self._parsers.pdf_definition_for(backend)
        if definition is None:
            raise ParserUnavailableError(
                f"parsing.pdf_parser {backend!r} has no single-engine pdf definition"
            )
        try:
            registry = self._parsers.build_registry(pdf_definition=definition)
        except ImportError as error:
            raise ParserUnavailableError(
                f"parsing.pdf_parser {backend!r} is not installed on this worker"
            ) from error
        self._pdf_parsers[backend] = registry
        return registry

    def chunker_for(self, run: RunSettings | None) -> BaseChunker:
        """Return the chunker a run chunks with.

        Args:
            run: Pinned settings, or ``None`` for the boot chunker.

        Returns:
            The boot chunker, or one built from the capped configuration.
        """
        cap = run.chunk_max_tokens if run is not None else None
        if cap is None:
            return self._chunker
        capped = cap_chunking_config(self._chunking.config, cap)
        if capped is self._chunking.config:
            return self._chunker
        cached = self._chunkers.get(cap)
        if cached is None:
            cached = self._build_chunker(capped)
            self._chunkers[cap] = cached
        return cached
