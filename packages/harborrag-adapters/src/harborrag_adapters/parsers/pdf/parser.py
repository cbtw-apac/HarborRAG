"""PDF-family orchestration, fallback, quality evaluation, and normalization."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import ClassVar, NoReturn

from harborrag_adapters.parsers.common.base import HarborParser
from harborrag_adapters.parsers.common.models import ParserAttempt, ParseRequest, ParseResult
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.errors import (
    EncryptedPdfError,
    MaxFileSizeExceededError,
    MaxPagesExceededError,
    NoExtractableTextError,
    PDFParsingFailedError,
)
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.config import (
    PDFParserProfile,
    PDFRouterConfig,
)
from harborrag_adapters.parsers.pdf.normalization import PDFNormalizer
from harborrag_adapters.parsers.pdf.parser_support import PDFParserSupportMixin
from harborrag_adapters.parsers.pdf.quality import PDFQualityEvaluator
from harborrag_adapters.parsers.pdf.router import PDFEngineRegistry, PDFEngineRouter
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("pdf")

_TYPED_ENGINE_FAILURES = (MaxPagesExceededError, MaxFileSizeExceededError, NoExtractableTextError)


def _raise_pdf_failure(attempts: list[ParserAttempt]) -> NoReturn:
    """Raise the shared typed cause when every attempt failed the same way.

    A caller configured with one engine (or every configured engine hitting
    the identical structural condition) gets the distinguishable typed error
    directly instead of the generic aggregate -- `PDFParsingFailedError`
    remains reserved for genuinely mixed or unclassified causes, where no
    single typed error would honestly describe every attempt.
    """
    first_cause = attempts[0].error if attempts else None
    if first_cause is not None and all(
        type(attempt.error) is type(first_cause) for attempt in attempts
    ):
        raise first_cause
    raise PDFParsingFailedError(attempts=attempts)


class HarborPDFParser(PDFParserSupportMixin, HarborParser):
    """Run the complete PDF workflow across independent provider engines."""

    parser_name: ClassVar[str] = "pdf"
    parser_version: ClassVar[str] = "1.0.0"
    extensions: frozenset[str] = frozenset({".pdf"})
    mime_types: frozenset[str] = frozenset({"application/pdf", "application/x-pdf"})

    def __init__(  # noqa: PLR0913
        self,
        *,
        engines: Sequence[HarborPDFEngine] | None = None,
        backends: Sequence[HarborPDFEngine] | None = None,
        min_content_chars: int = 20,
        profile: PDFParserProfile | str = PDFParserProfile.BALANCED,
        router: PDFEngineRouter | None = None,
        router_config: PDFRouterConfig | None = None,
        quality_evaluator: PDFQualityEvaluator | None = None,
        normalizer: PDFNormalizer | None = None,
    ) -> None:
        if engines is not None and backends is not None:
            raise ValueError("Pass PDF engines or backends, not both")

        self.profile = self._normalize_profile(profile, router_config)
        profile_name = self._profile_name
        configured_engines = engines if engines is not None else backends
        if configured_engines is None:
            raise ValueError(
                "PDF engines are required; construct configured parsers with HarborParserFactory"
            )
        engine_list = list(configured_engines)
        self.min_content_chars = min_content_chars
        self._registry = PDFEngineRegistry(tuple(engine_list))
        if router is None:
            resolved_router_config = router_config or self._router_config(
                profile_name,
                explicit_order=(
                    tuple(engine.name for engine in engine_list)
                    if configured_engines is not None
                    else None
                ),
            )
            router = PDFEngineRouter(self._registry, resolved_router_config)
        self._router = router
        self._quality_evaluator = quality_evaluator or PDFQualityEvaluator(min_content_chars)
        self._normalizer = normalizer or PDFNormalizer()

    @property
    def engines(self) -> tuple[HarborPDFEngine, ...]:
        return self._registry.available()

    @property
    def backends(self) -> list[HarborPDFEngine]:
        return list(self.engines)

    async def parse(self, request: ParseRequest) -> ParseResult:
        attempts: list[ParserAttempt] = []
        warnings: list[str] = []
        minimum_score = self._router.minimum_quality_score(request)

        if self._is_empty_request(request):
            parser_logger.info(
                "PDF input %s is empty (0 bytes); returning empty content",
                request.filename or request.source_uri,
            )
            metadata = request.options.get("metadata", {})
            return self._normalizer.normalize(
                result=self._empty_result(),
                attempts=[self._empty_attempt()],
                metadata=dict(metadata) if isinstance(metadata, dict) else {},
                warnings=[],
            )
        self._guard_request_size(request)

        for engine in self._router.resolve_candidates(request):
            started = perf_counter()
            try:
                result = await engine.parse(request)
            except EncryptedPdfError:
                raise
            except _TYPED_ENGINE_FAILURES as error:
                # A configured limit or no-extractable-text condition is
                # specific to this engine's config, not necessarily fatal for
                # a different engine in the chain (e.g. an OCR-capable engine
                # may still succeed) -- record the typed cause and continue,
                # unlike `EncryptedPdfError` above.
                duration_ms = (perf_counter() - started) * 1000
                message = str(error)
                attempts.append(
                    ParserAttempt(
                        engine=engine.name,
                        success=False,
                        duration_ms=duration_ms,
                        message=message,
                        error=error,
                    )
                )
                warnings.append(f"{engine.name}: {message}")
                continue
            except ImportError as error:
                duration_ms = (perf_counter() - started) * 1000
                message = f"unavailable ({error})"
                attempts.append(
                    ParserAttempt(
                        engine=engine.name,
                        success=False,
                        duration_ms=duration_ms,
                        message=message,
                    )
                )
                warnings.append(f"{engine.name}: {message}")
                continue
            except Exception as error:  # noqa: BLE001
                duration_ms = (perf_counter() - started) * 1000
                message = f"failed ({error})"
                attempts.append(
                    ParserAttempt(
                        engine=engine.name,
                        success=False,
                        duration_ms=duration_ms,
                        message=message,
                    )
                )
                warnings.append(f"{engine.name}: {message}")
                parser_logger.warning(
                    "PDF engine %s failed for %s: %s",
                    engine.name,
                    request.filename or request.source_uri,
                    error,
                )
                continue

            duration_ms = (perf_counter() - started) * 1000
            quality = self._quality_evaluator.evaluate(
                result,
                minimum_score=minimum_score,
            )
            attempts.append(
                ParserAttempt(
                    engine=engine.name,
                    success=True,
                    duration_ms=duration_ms,
                    quality_score=quality.score,
                    message=quality.message,
                )
            )
            warnings.extend(result.warnings)
            if quality.accepted:
                metadata = request.options.get("metadata", {})
                return self._normalizer.normalize(
                    result=result,
                    attempts=attempts,
                    metadata=dict(metadata) if isinstance(metadata, dict) else {},
                    warnings=warnings,
                )
            warnings.append(f"{engine.name}: {quality.message}")

        _raise_pdf_failure(attempts)

    def supports(self, source: Path, mime_type: str | None = None) -> bool:
        normalized_mime = (mime_type or "").partition(";")[0].strip().lower()
        return source.suffix.lower() in self.extensions or (
            bool(normalized_mime) and normalized_mime in self.mime_types
        )

    def parse_input(self, input: ParseInput) -> ParsedDocument:
        request = ParseRequest(
            source_uri=str(input.path or input.filename or "memory://pdf"),
            filename=input.filename,
            mime_type=input.content_type,
            options={
                "profile": self._profile_name,
                "metadata": input.metadata,
            },
        )
        attempts: list[ParserAttempt] = []
        warnings: list[str] = []
        minimum_score = self._router.minimum_quality_score(request)

        if self._is_empty_source(input):
            # An empty source has no content for any engine to reject or
            # accept -- routing it through the fallback chain just collects
            # N confusing per-engine failures ("could not open PDF", "invalid
            # PDF format", ...) before raising `PDFParsingFailedError`. There
            # is nothing to parse, so succeed with empty output instead of
            # treating a 0-byte file as a hard failure.
            parser_logger.info(
                "PDF input %s is empty (0 bytes); returning empty content",
                input_label(input),
                extra=parser_log_extra(
                    input=input,
                    parser_name=self.parser_name,
                    profile=self._profile_name,
                ),
            )
            return self._normalizer.normalize_document(
                parse_input=input,
                result=self._empty_result(),
                profile=self._profile_name,
                attempts=[self._empty_attempt()],
                warnings=[],
            )
        self._guard_size(input)

        for engine in self._router.resolve_candidates(request):
            parser_logger.debug(
                "Trying PDF engine %s for %s",
                engine.name,
                input_label(input),
                extra=parser_log_extra(
                    input=input,
                    parser_name=self.parser_name,
                    parser_engine=engine.name,
                    profile=self._profile_name,
                ),
            )
            started = perf_counter()
            try:
                result = engine.parse_input(input)
            except EncryptedPdfError:
                raise
            except _TYPED_ENGINE_FAILURES as error:
                # See the matching branch in `parse()` above: a configured
                # limit or no-extractable-text condition isn't necessarily
                # fatal for a different engine in the fallback chain.
                duration_ms = (perf_counter() - started) * 1000
                message = str(error)
                attempts.append(
                    ParserAttempt(
                        engine=engine.name,
                        success=False,
                        duration_ms=duration_ms,
                        message=message,
                        error=error,
                    )
                )
                warnings.append(f"{engine.name}: {message}")
                continue
            except ImportError as error:
                duration_ms = (perf_counter() - started) * 1000
                message = f"unavailable ({error})"
                attempts.append(
                    ParserAttempt(
                        engine=engine.name,
                        success=False,
                        duration_ms=duration_ms,
                        message=message,
                    )
                )
                warnings.append(f"{engine.name}: {message}")
                continue
            except Exception as error:  # noqa: BLE001
                duration_ms = (perf_counter() - started) * 1000
                message = f"failed ({error})"
                attempts.append(
                    ParserAttempt(
                        engine=engine.name,
                        success=False,
                        duration_ms=duration_ms,
                        message=message,
                    )
                )
                warnings.append(f"{engine.name}: {message}")
                parser_logger.warning(
                    "PDF engine %s failed for %s: %s",
                    engine.name,
                    input_label(input),
                    error,
                )
                continue

            duration_ms = (perf_counter() - started) * 1000
            quality = self._quality_evaluator.evaluate(
                result,
                minimum_score=minimum_score,
            )
            attempts.append(
                ParserAttempt(
                    engine=engine.name,
                    success=True,
                    duration_ms=duration_ms,
                    quality_score=quality.score,
                    message=quality.message,
                )
            )
            warnings.extend(result.warnings)
            if quality.accepted:
                return self._normalizer.normalize_document(
                    parse_input=input,
                    result=result,
                    profile=self._profile_name,
                    attempts=attempts,
                    warnings=warnings,
                )
            warnings.append(f"{engine.name}: {quality.message}")

        _raise_pdf_failure(attempts)


PdfParser = HarborPDFParser
PdfParserProfile = PDFParserProfile
