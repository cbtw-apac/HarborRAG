"""Complete markup-family parsing workflow."""

from __future__ import annotations

from harborrag_adapters.parsers.common.family import (
    HarborSingleEngineFamilyParser,
    SingleEngineRouter,
)
from harborrag_adapters.parsers.markup.base import HarborMarkupEngine
from harborrag_adapters.parsers.markup.normalization import MarkupNormalizer


class HarborMarkupParser(HarborSingleEngineFamilyParser):
    """Select an engine for human-authored markup documents."""

    parser_name = "markup"

    def __init__(
        self,
        engines: tuple[HarborMarkupEngine, ...],
    ) -> None:
        super().__init__(SingleEngineRouter("markup", engines), MarkupNormalizer())
