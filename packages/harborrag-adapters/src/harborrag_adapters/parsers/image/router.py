"""Image OCR engine selection policy."""

from __future__ import annotations

from harborrag_adapters.parsers.common.family import SingleEngineRouter
from harborrag_adapters.parsers.image.base import HarborImageEngine


class ImageEngineRouter(SingleEngineRouter):
    """Select an OCR provider by image format or explicit engine name."""

    def __init__(self, engines: tuple[HarborImageEngine, ...]) -> None:
        super().__init__("image", engines)
