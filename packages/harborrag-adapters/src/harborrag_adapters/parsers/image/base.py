"""Image OCR engine contract."""

from __future__ import annotations

from abc import ABC
from typing import ClassVar

from harborrag_adapters.parsers.common.base import HarborParserEngine
from harborrag_core.domain.parser import ParsedDocument, ParseInput


class HarborImageEngine(HarborParserEngine[ParseInput, ParsedDocument], ABC):
    """Provider contract for raster-image OCR engines."""

    supports_orientation: ClassVar[bool] = False
