"""Presentation engine contract."""

from __future__ import annotations

from abc import ABC
from typing import ClassVar

from harborrag_adapters.parsers.common.base import HarborParserEngine
from harborrag_core.domain.parser import ParsedDocument, ParseInput


class HarborPresentationEngine(
    HarborParserEngine[ParseInput, ParsedDocument],
    ABC,
):
    """Provider contract for slides, notes, and presentation layouts."""

    supports_speaker_notes: ClassVar[bool] = False
