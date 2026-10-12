"""Compose the default structure-aware refiner."""

from __future__ import annotations

import importlib.util
import logging

from harborrag_core.contracts.chunking import TokenCounter

from .code_structure import CodeStructureRefiner, ParserLoader
from .document_structure import DocumentStructureRefiner
from .router import RoutingTextRefiner
from .separators import SeparatorRefiner
from .text_structure import TextStructureRefiner

logger = logging.getLogger(__name__)


def build_text_refiner(
    token_counter: TokenCounter,
    *,
    parser_loader: ParserLoader | None = None,
) -> RoutingTextRefiner:
    """Build the routing refiner engine uses for oversized units.

    Every strategy shares ``token_counter``. Without the ``chunking-code``
    extra, code falls back to separator splitting; nothing here requires an
    optional dependency.
    """

    if parser_loader is None and importlib.util.find_spec("tree_sitter") is None:
        logger.warning(
            "tree-sitter is not installed; code chunks split at separators. "
            "Install harborrag-adapters[chunking-code] for syntax-aware splitting."
        )
    return RoutingTextRefiner(
        text=TextStructureRefiner(token_counter),
        document=DocumentStructureRefiner(token_counter),
        code=CodeStructureRefiner(token_counter, parser_loader=parser_loader),
        separators=SeparatorRefiner(token_counter),
    )


def text_refiner_fingerprint() -> str:
    """Identify the default refiner's boundary behaviour without building it.

    Independent of installed extras, so API and worker processes agree on the
    processing identity.
    """

    return build_text_refiner(_NeverCalledCounter(), parser_loader=_no_parser).fingerprint()


class _NeverCalledCounter:
    def count(self, text: str) -> int:
        raise AssertionError("fingerprinting must not count tokens")


def _no_parser(language: str) -> None:
    return None
