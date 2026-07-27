"""PDF engine registration and configuration-driven selection policy."""

from __future__ import annotations

from harborrag_adapters.parsers.common.models import ParseRequest
from harborrag_adapters.parsers.errors import (
    DuplicatePDFEngineError,
    UnknownPDFEngineError,
)
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.config import PDFRouterConfig


class PDFEngineRegistry:
    """Named collection of independent PDF provider engines."""

    def __init__(self, engines: tuple[HarborPDFEngine, ...] = ()) -> None:
        self._engines: dict[str, HarborPDFEngine] = {}
        for engine in engines:
            self.register(engine)

    def register(self, engine: HarborPDFEngine) -> None:
        if engine.name in self._engines:
            raise DuplicatePDFEngineError(engine.name)
        self._engines[engine.name] = engine

    def get(self, name: str) -> HarborPDFEngine:
        try:
            return self._engines[name]
        except KeyError as error:
            raise UnknownPDFEngineError(name) from error

    def available(self) -> tuple[HarborPDFEngine, ...]:
        return tuple(self._engines.values())


class PDFEngineRouter:
    """Apply an explicit engine or named profile to the PDF engine registry."""

    def __init__(
        self,
        registry: PDFEngineRegistry,
        config: PDFRouterConfig,
    ) -> None:
        self._registry = registry
        self._config = config

    def resolve_candidates(
        self,
        request: ParseRequest,
    ) -> tuple[HarborPDFEngine, ...]:
        if request.engine:
            return (self._registry.get(request.engine),)

        profile_name = str(request.options.get("profile", self._config.default_profile)).lower()
        try:
            profile = self._config.profiles[profile_name]
        except KeyError as error:
            raise ValueError(f"Unknown PDF parser profile: {profile_name!r}") from error
        return tuple(self._registry.get(name) for name in profile.engine_order)

    def minimum_quality_score(self, request: ParseRequest) -> float:
        profile_name = str(request.options.get("profile", self._config.default_profile)).lower()
        try:
            return self._config.profiles[profile_name].minimum_quality_score
        except KeyError as error:
            raise ValueError(f"Unknown PDF parser profile: {profile_name!r}") from error
