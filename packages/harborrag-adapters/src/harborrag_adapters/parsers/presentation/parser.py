"""Complete presentation-family parsing workflow."""

from __future__ import annotations

from harborrag_adapters.parsers.common.family import (
    FamilyResultNormalizer,
    HarborSingleEngineFamilyParser,
    SingleEngineRouter,
)
from harborrag_adapters.parsers.presentation.base import HarborPresentationEngine


class HarborPresentationParser(HarborSingleEngineFamilyParser):
    """Parse presentations through independently configurable engines."""

    parser_name = "presentation"

    def __init__(
        self,
        engines: tuple[HarborPresentationEngine, ...],
    ) -> None:
        super().__init__(
            SingleEngineRouter("presentation", engines),
            FamilyResultNormalizer("presentation"),
        )
