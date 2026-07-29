"""PDF-family orchestration, fallback, quality evaluation, and normalization."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import ClassVar

from harborrag_adapters.parsers.common.base import HarborParser
from harborrag_adapters.parsers.common.models import ParserAttempt, ParseRequest, ParseResult
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.errors import (
    EncryptedPdfError,
    PDFParsingFailedError,
)
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.config import (
    PDFParserProfile,
    PDFProfileConfig,
    PDFRouterConfig,
)
from harborrag_adapters.parsers.pdf.normalization import PDFNormalizer
from harborrag_adapters.parsers.pdf.quality import PDFQualityEvaluator
from harborrag_adapters.parsers.pdf.router import PDFEngineRegistry, PDFEngineRouter
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("pdf")


class HarborPDFParser(HarborParser):
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

        for engine in self._router.resolve_candidates(request):
            started = perf_counter()
            try:
                result = await engine.parse(request)
            except EncryptedPdfError:
                raise
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

        raise PDFParsingFailedError(attempts=attempts)

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

        raise PDFParsingFailedError(attempts=attempts)

    @staticmethod
    def _router_config(
        profile: str,
        *,
        explicit_order: tuple[str, ...] | None,
    ) -> PDFRouterConfig:
        profile_name = profile
        if explicit_order is None:
            return PDFRouterConfig(default_profile=profile_name)
        return PDFRouterConfig(
            default_profile=profile_name,
            profiles={
                profile_name: PDFProfileConfig(
                    explicit_order,
                    minimum_quality_score=PDFRouterConfig()
                    .profiles[profile_name]
                    .minimum_quality_score,
                )
            },
        )

    @property
    def _profile_name(self) -> str:
        if isinstance(self.profile, PDFParserProfile):
            return self.profile.value
        return self.profile

    @staticmethod
    def _normalize_profile(
        profile: PDFParserProfile | str,
        router_config: PDFRouterConfig | None,
    ) -> PDFParserProfile | str:
        try:
            return PDFParserProfile.normalize(profile)
        except ValueError:
            profile_name = str(profile).lower().strip()
            if router_config is not None and profile_name in router_config.profiles:
                return profile_name
            raise
