"""Options for the LLM PDF engine: which chat model reads the pages, and how much."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from harborrag_adapters.parsers.common.option_rules import (
    TEXT,
    OptionRule,
    integer_from,
    require_permitted_options,
)

DEFAULT_MAX_PAGES = 50
DEFAULT_DPI = 150
MIN_MAX_PAGES = 1
_DPI_RANGE = range(72, 301)
_OPTION_RULES: Mapping[str, OptionRule] = {
    "model": TEXT,
    "max_pages": integer_from(MIN_MAX_PAGES),
    "dpi": integer_from(_DPI_RANGE.start),
}


@dataclass(frozen=True, slots=True)
class LlmPDFConfig:
    """Options for transcribing PDF pages with a multimodal chat model.

    Attributes:
        model: Logical chat model from models.yaml. Every deployment behind it
            must declare ``multimodal: true``; the runtime checks this at startup.
        max_pages: Most pages one PDF may send, at one model call per page. A
            longer PDF is left to the next engine in the chain.
        dpi: Resolution pages are rendered at before sending, from 72 to 300.
    """

    model: str
    max_pages: int = DEFAULT_MAX_PAGES
    dpi: int = DEFAULT_DPI

    def __post_init__(self) -> None:
        """Reject a blank model name and limits outside their types or ranges.

        Raises:
            ValueError: A setting has the wrong type, is blank or is out of range.
        """
        require_permitted_options(
            "LLM PDF",
            {name: getattr(self, name) for name in _OPTION_RULES},
            _OPTION_RULES,
        )
        if not self.model.strip():
            raise ValueError("model must name a chat model from models.yaml")
        if self.dpi not in _DPI_RANGE:
            raise ValueError(f"dpi must be between {_DPI_RANGE.start} and {_DPI_RANGE.stop - 1}")
