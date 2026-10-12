"""Structure-aware text refiners implementing core ``TextRefiner``.

Engine segments canonical documents and calls a refiner only for a unit that
is still above its hard token limit. ``build_text_refiner`` returns the
routing refiner that picks a code, document, text or separator strategy for
that unit; each strategy only proposes cut positions and the shared base
enforces limits, coverage, and source spans.
"""

from harborrag_adapters.chunking.code_structure import CodeStructureRefiner
from harborrag_adapters.chunking.document_structure import DocumentStructureRefiner
from harborrag_adapters.chunking.factory import build_text_refiner, text_refiner_fingerprint
from harborrag_adapters.chunking.languages import guess_language, normalize_language
from harborrag_adapters.chunking.recursive import RecursiveTextRefiner
from harborrag_adapters.chunking.router import RoutingTextRefiner
from harborrag_adapters.chunking.separators import SeparatorRefiner
from harborrag_adapters.chunking.text_structure import TextStructureRefiner
from harborrag_adapters.chunking.tokenizers import TiktokenTokenCounter

__all__ = [
    "CodeStructureRefiner",
    "DocumentStructureRefiner",
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
