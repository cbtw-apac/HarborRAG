"""Chunking orchestration and dependency composition."""

from .composition import build_chunking_service
from .service import ChunkingService

__all__ = ["ChunkingService", "build_chunking_service"]
