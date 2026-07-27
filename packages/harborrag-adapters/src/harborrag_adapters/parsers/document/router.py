"""Engine lookup policy owned by the document parser family."""

from __future__ import annotations

from harborrag_adapters.parsers.common.family import SingleEngineRouter
from harborrag_adapters.parsers.document.base import HarborDocumentEngine


class DocumentEngineRouter(SingleEngineRouter):
    """Select a document provider by extension, MIME type, or explicit name."""

    def __init__(self, engines: tuple[HarborDocumentEngine, ...]) -> None:
        super().__init__("document", engines)
