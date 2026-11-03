"""Compose the default structure-aware refiner."""

from __future__ import annotations

import importlib.metadata
import logging

from harborrag_core.contracts.chunking import TokenCounter

from .code_structure import CodeStructureRefiner, ParserLoader
from .document_structure import DocumentStructureRefiner
from .languages import GRAMMAR_SET
from .router import RoutingTextRefiner
from .separators import SeparatorRefiner
from .text_structure import TextStructureRefiner

logger = logging.getLogger(__name__)


class GrammarSetMismatch(ImportError):
    """The installed tree-sitter packages differ from the declared ``GRAMMAR_SET``."""


def build_text_refiner(
    token_counter: TokenCounter,
    *,
    parser_loader: ParserLoader | None = None,
    require_grammars: bool = True,
) -> RoutingTextRefiner:
    """Build the routing refiner engine uses for oversized units.

    Every strategy shares ``token_counter``. The refiner's fingerprint declares
    the grammar set (``GRAMMAR_SET``) whether or not it is installed, so the
    same fingerprint must mean the same splits: with the default parser loader
    this raises :class:`GrammarSetMismatch` when tree-sitter or a grammar is
    missing or installed at another minor version, instead of quietly
    splitting code at separators under the same identity. Pass
    ``require_grammars=False`` to accept that fallback (it is logged); the
    fingerprint then no longer guarantees the same code splits across
    environments, so deployments that ingest should install the grammars and
    keep the default. Pass a ``parser_loader`` to take control of parsing.
    """

    if parser_loader is None:
        problems = grammar_set_problems()
        if problems and require_grammars:
            raise GrammarSetMismatch(
                "installed tree-sitter packages do not match the declared grammar set "
                f"({'; '.join(problems)}); install harborrag-adapters[chunking-code] or pass "
                "require_grammars=False to split code at separators"
            )
        if problems:
            logger.warning(
                "code chunks may split at separators (%s); install harborrag-adapters[chunking-code] "
                "for syntax-aware splitting",
                "; ".join(problems),
            )
    return RoutingTextRefiner(
        text=TextStructureRefiner(token_counter),
        document=DocumentStructureRefiner(token_counter),
        code=CodeStructureRefiner(token_counter, parser_loader=parser_loader),
        separators=SeparatorRefiner(token_counter),
    )


def grammar_set_problems() -> list[str]:
    """Differences between the installed tree-sitter packages and ``GRAMMAR_SET``.

    Each entry names a package that is missing or whose major.minor version is
    not the declared one; an empty list means splits match the fingerprint.
    """

    problems: list[str] = []
    for entry in GRAMMAR_SET.split(";"):
        name, _, declared = entry.rpartition("-")
        package = name if name == "tree-sitter" else f"tree-sitter-{name}"
        try:
            installed = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{package} missing")
            continue
        if ".".join(installed.split(".")[:2]) != declared:
            problems.append(f"{package} {installed} (declared {declared})")
    return problems


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
