"""Stable request, result, element, and attempt models for every parser family."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument


@dataclass(slots=True)
class ParsedElement:
    """One normalized content element independent of a provider schema."""

    element_type: str
    content: str
    page_number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParseRequest:
    """Source identity, routing hints, and caller-selected parser policy."""

    source_uri: str
    filename: str | None = None
    mime_type: str | None = None
    parser: str | None = None
    engine: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParserAttempt:
    """One provider attempt recorded by a family fallback workflow."""

    engine: str
    success: bool
    duration_ms: float
    quality_score: float | None = None
    message: str | None = None
    # The typed exception this attempt failed with, when one is known (e.g. a
    # configured limit or a no-extractable-text condition). Lets a caller
    # raise that specific type when every attempt shares the same cause,
    # instead of only ever seeing the generic aggregate failure.
    error: BaseException | None = None


@dataclass(slots=True)
class ParseResult:
    """Normalized output consumed by ingestion regardless of provider."""

    elements: list[ParsedElement]
    text: str
    metadata: dict[str, Any]
    parser_name: str
    engine_name: str
    attempts: list[ParserAttempt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_parsed_document(
        cls,
        document: ParsedDocument,
        *,
        engine_name: str,
        attempts: list[ParserAttempt] | None = None,
    ) -> ParseResult:
        metadata = dict(document.metadata or {})
        return cls(
            elements=[
                ParsedElement(
                    element_type=element.type,
                    content=element.content or "",
                    page_number=_page_number(element),
                    metadata=dict(element.metadata),
                )
                for element in document.elements or ()
            ],
            text=document.content,
            metadata=metadata,
            parser_name=document.parser_name,
            engine_name=engine_name,
            attempts=list(attempts or ()),
            warnings=list(document.warnings or ()),
        )

    def to_parsed_document(self) -> ParsedDocument:
        return ParsedDocument(
            content=self.text,
            parser_name=self.parser_name,
            elements=[
                DocumentElement(
                    id=f"{self.parser_name}:{index}",
                    type=_element_type(element.element_type),
                    content=element.content,
                    metadata={
                        **element.metadata,
                        **(
                            {"page_number": element.page_number}
                            if element.page_number is not None
                            else {}
                        ),
                    },
                )
                for index, element in enumerate(self.elements)
            ],
            metadata={
                **self.metadata,
                "engine_name": self.engine_name,
                "parser_attempts": [
                    {
                        "engine": attempt.engine,
                        "success": attempt.success,
                        "duration_ms": attempt.duration_ms,
                        "quality_score": attempt.quality_score,
                        "message": attempt.message,
                    }
                    for attempt in self.attempts
                ],
            },
            warnings=self.warnings or None,
        )


def _page_number(element: DocumentElement) -> int | None:
    value = element.metadata.get("page_number", element.metadata.get("page"))
    return value if isinstance(value, int) else None


def _element_type(value: str) -> Any:
    return value
