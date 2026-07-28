"""Complete image-family OCR workflow."""

from __future__ import annotations

from harborrag_adapters.parsers.common.family import (
    FamilyResultNormalizer,
    HarborSingleEngineFamilyParser,
)
from harborrag_adapters.parsers.image.base import HarborImageEngine
from harborrag_adapters.parsers.image.router import ImageEngineRouter


class HarborImageParser(HarborSingleEngineFamilyParser):
    """Route images to an OCR provider and normalize extracted text."""

    parser_name = "image"

    def __init__(
        self,
        engines: tuple[HarborImageEngine, ...],
    ) -> None:
        super().__init__(
            ImageEngineRouter(engines),
            FamilyResultNormalizer("image"),
        )

    @property
    def ocr_engine(self) -> str:
        engine = self.engines[0]
        return str(
            getattr(
                engine,
                "ocr_engine",
                getattr(engine, "parser_engine", engine.name),
            )
        )

    @property
    def parser_engine(self) -> str:
        return self.ocr_engine
