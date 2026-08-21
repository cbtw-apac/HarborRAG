"""Per-source sample documents for the deterministic graph eval corpus.

The projector is selected by the *chunk's* connector type, which
``CanonicalChunkFactory._connector_type`` reads from ``provenance.extra["connector_type"]``
before the chunking request field -- so every directory's ``_defaults.json`` carries it
there, and ``build_corpus`` routes the request with the same value.

Additive by rule: the four ``local`` documents and every provider sample already named by
a golden expectation are pinned. New samples are new files.
"""

from .loader import DEFAULTS_NAME, ELEMENT_TYPES, FIXTURES, eval_documents, load_document

__all__ = [
    "DEFAULTS_NAME",
    "ELEMENT_TYPES",
    "FIXTURES",
    "eval_documents",
    "load_document",
]
