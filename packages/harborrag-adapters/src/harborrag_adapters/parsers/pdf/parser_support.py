"""Input guards and profile helpers for PDF parser orchestration."""

from __future__ import annotations

from harborrag_adapters.parsers.common.models import ParserAttempt, ParseRequest
from harborrag_adapters.parsers.common.resources import request_to_parse_input
from harborrag_adapters.parsers.common.validation import (
    guard_parse_input_size,
    parse_input_is_empty,
)
from harborrag_adapters.parsers.pdf.config import (
    PDFParserProfile,
    PDFProfileConfig,
    PDFRouterConfig,
)
from harborrag_adapters.parsers.pdf.models import PDFParseResult
from harborrag_core.domain.parser import ParseInput

_EMPTY_INPUT_ENGINE = "empty-input"


class PDFParserSupportMixin:
    """Provide small reusable guard and profile operations for the PDF parser."""

    profile: PDFParserProfile | str

    @staticmethod
    def _is_empty_request(request: ParseRequest) -> bool:
        """Detect a zero-byte request without changing ambiguous-input behavior."""

        try:
            parse_input = request_to_parse_input(request)
        except (TypeError, ValueError):
            return False
        return parse_input_is_empty(parse_input)

    @staticmethod
    def _is_empty_source(input: ParseInput) -> bool:
        """Detect a zero-byte ParseInput while leaving unreadable paths to engines."""

        try:
            return parse_input_is_empty(input)
        except (OSError, ValueError):
            return False

    @staticmethod
    def _guard_size(input: ParseInput) -> None:
        """Reject a positively identified oversized source before routing."""

        try:
            guard_parse_input_size(input)
        except OSError:
            pass

    @classmethod
    def _guard_request_size(cls, request: ParseRequest) -> None:
        try:
            parse_input = request_to_parse_input(request)
        except (TypeError, ValueError):
            return
        cls._guard_size(parse_input)

    @staticmethod
    def _empty_result() -> PDFParseResult:
        return PDFParseResult(content="", engine=_EMPTY_INPUT_ENGINE, quality_score=1.0)

    @staticmethod
    def _empty_attempt() -> ParserAttempt:
        return ParserAttempt(
            engine=_EMPTY_INPUT_ENGINE,
            success=True,
            duration_ms=0.0,
            quality_score=1.0,
            message="input is empty (0 bytes); no engine attempted",
        )

    @staticmethod
    def _router_config(
        profile: str,
        *,
        explicit_order: tuple[str, ...] | None,
    ) -> PDFRouterConfig:
        if explicit_order is None:
            return PDFRouterConfig(default_profile=profile)
        return PDFRouterConfig(
            default_profile=profile,
            profiles={
                profile: PDFProfileConfig(
                    explicit_order,
                    minimum_quality_score=PDFRouterConfig().profiles[profile].minimum_quality_score,
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
