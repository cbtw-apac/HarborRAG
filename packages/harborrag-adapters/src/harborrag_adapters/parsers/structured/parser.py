"""Complete structured-data parsing workflow."""

from __future__ import annotations

from harborrag_adapters.parsers.common.family import (
    FamilyResultNormalizer,
    HarborSingleEngineFamilyParser,
    SingleEngineRouter,
)
from harborrag_adapters.parsers.structured.base import HarborStructuredEngine


class HarborStructuredParser(HarborSingleEngineFamilyParser):
    """Select and run a structured-data provider engine."""

    parser_name = "structured"

    def __init__(
        self,
        engines: tuple[HarborStructuredEngine, ...],
    ) -> None:
        super().__init__(
            SingleEngineRouter("structured", engines),
            FamilyResultNormalizer("structured"),
        )
