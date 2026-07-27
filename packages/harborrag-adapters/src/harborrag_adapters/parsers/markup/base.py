"""Markup engine contract."""

from __future__ import annotations

from abc import ABC

from harborrag_adapters.parsers.common.base import HarborParserEngine
from harborrag_core.domain.parser import ParsedDocument, ParseInput


class HarborMarkupEngine(HarborParserEngine[ParseInput, ParsedDocument], ABC):
    """Provider contract for HTML, Markdown, XML, and related markup."""
