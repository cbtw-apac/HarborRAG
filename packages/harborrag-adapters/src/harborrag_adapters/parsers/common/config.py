"""Cross-family parser configuration containers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ParserFamilyConfig:
    """Common switch and free-form options shared by configured families."""

    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParserConfig:
    """Configured family sections accepted by ``HarborParserFactory``."""

    pdf: Any = None
    document: ParserFamilyConfig = field(default_factory=ParserFamilyConfig)
    spreadsheet: ParserFamilyConfig = field(default_factory=ParserFamilyConfig)
    presentation: ParserFamilyConfig = field(default_factory=ParserFamilyConfig)
    markup: ParserFamilyConfig = field(default_factory=ParserFamilyConfig)
    structured: ParserFamilyConfig = field(default_factory=ParserFamilyConfig)
    text: ParserFamilyConfig = field(default_factory=ParserFamilyConfig)
    image: ParserFamilyConfig = field(default_factory=ParserFamilyConfig)
