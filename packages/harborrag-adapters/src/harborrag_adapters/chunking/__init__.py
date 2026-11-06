"""Structure-aware text refiners implementing core ``TextRefiner``.

Engine segments canonical documents and calls a refiner only for a unit that
is still above its hard token limit. ``build_text_refiner`` returns the
routing refiner that picks a code, document, text or separator strategy for
that unit; each strategy only proposes cut positions and the shared base
enforces limits, coverage, and source spans. ``MarkdownChunker`` chunks a
whole Markdown file: it splits at sections first and refines each one.

Importing this package needs no optional dependency: tree-sitter grammars
(``chunking-code``), tiktoken (``chunking-tokens``) and LangChain
(``chunking``) are loaded only when used.
"""

from .code_structure import CodeStructureRefiner
from .document_structure import DocumentStructureRefiner
from .factory import GrammarSetMismatch, build_text_refiner, text_refiner_fingerprint
from .languages import guess_language, normalize_language
from .markdown_sections import ChunkRole, MarkdownChunk, MarkdownChunker
from .recursive import RecursiveTextRefiner
from .router import RoutingTextRefiner
from .separators import SeparatorRefiner
from .text_structure import TextStructureRefiner
from .tokenizers import TiktokenTokenCounter

__all__ = [
    "ChunkRole",
    "CodeStructureRefiner",
    "DocumentStructureRefiner",
    "GrammarSetMismatch",
    "MarkdownChunk",
    "MarkdownChunker",
    "RecursiveTextRefiner",
    "RoutingTextRefiner",
    "SeparatorRefiner",
    "TextStructureRefiner",
    "TiktokenTokenCounter",
    "build_text_refiner",
    "guess_language",
    "normalize_language",
    "text_refiner_fingerprint",
]
