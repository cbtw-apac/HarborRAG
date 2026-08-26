"""Plain-text and source-code engine contract."""

from __future__ import annotations

from abc import ABC

from harborrag_adapters.parsers.common.base import HarborParserEngine
from harborrag_core.domain.parser import ParsedDocument, ParseInput


class HarborTextEngine(HarborParserEngine[ParseInput, ParsedDocument], ABC):
    """Provider contract for plain text, logs, configuration, and source code."""
