from __future__ import annotations

from dataclasses import dataclass

from .config import ChunkingConfig
from .schemas import ChunkingRequest


@dataclass(frozen=True, slots=True)
class SelectedChunkRoute:
    """Resolved strategy and profile names for one chunking request."""

    strategy: str
    profile: str


class ChunkingRouter:
    """Resolve one named profile without executing its strategy."""

    def __init__(self, config: ChunkingConfig) -> None:
        self._config = config

    def select(self, request: ChunkingRequest) -> SelectedChunkRoute:
        """Resolve the configured route for a chunking request."""

        if request.profile_name is not None:
            return self._from_profile(request.profile_name)
        content_category = self._content_category(request)
        for route in self._config.routes:
            if route.matches(
                source_kind=request.source_kind,
                content_type=request.content_type,
                content_category=content_category,
            ):
                return self._from_profile(route.profile)
        return self._from_profile(self._config.default_profile)

    def _from_profile(self, profile_name: str) -> SelectedChunkRoute:
        try:
            profile = self._config.profiles[profile_name]
        except KeyError as exc:
            raise ValueError(f"unknown chunking profile: {profile_name}") from exc
        return SelectedChunkRoute(strategy=profile.strategy, profile=profile_name)

    @staticmethod
    def _content_category(request: ChunkingRequest) -> str:
        if request.content_type in {
            "application/json",
            "application/jsonl",
            "application/x-ndjson",
        }:
            return "structured_data"
        if request.content_type in {
            "application/epub+zip",
            "application/pdf",
            "application/xhtml+xml",
            "document",
            "page",
            "text/html",
            "text/markdown",
            "text/x-markdown",
        } or request.content_type.startswith("application/vnd.openxmlformats-officedocument."):
            return "document"
        element_types = {element.type for element in request.document.content if element.content}
        if element_types and element_types <= {"table"}:
            return "table"
        if "code" in element_types:
            return "source_code"
        if element_types and element_types <= {"paragraph"}:
            return "plain_text"
        return "document"
