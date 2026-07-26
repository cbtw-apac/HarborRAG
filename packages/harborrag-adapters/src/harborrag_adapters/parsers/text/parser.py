"""Complete text-family parsing workflow."""

from __future__ import annotations

from harborrag_adapters.parsers.common.family import (
    FamilyResultNormalizer,
    HarborSingleEngineFamilyParser,
    SingleEngineRouter,
)
from harborrag_adapters.parsers.text.base import HarborTextEngine


class HarborTextParser(HarborSingleEngineFamilyParser):
    """Parse plain text and source-like documents."""

    parser_name = "text"

    def __init__(self, engines: tuple[HarborTextEngine, ...]) -> None:
        super().__init__(
            SingleEngineRouter("text", engines),
            FamilyResultNormalizer("text"),
        )
