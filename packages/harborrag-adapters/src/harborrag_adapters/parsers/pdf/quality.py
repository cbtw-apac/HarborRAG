"""PDF-specific content quality evaluation."""

from __future__ import annotations

from harborrag_adapters.parsers.common.quality import QualityAssessment
from harborrag_adapters.parsers.pdf.models import PDFParseResult


class PDFQualityEvaluator:
    """Score PDF output using text sufficiency without provider assumptions."""

    def __init__(self, min_content_chars: int = 20) -> None:
        if min_content_chars < 0:
            raise ValueError("PDF min_content_chars cannot be negative")
        self._min_content_chars = min_content_chars

    def evaluate(
        self,
        result: PDFParseResult,
        *,
        minimum_score: float,
    ) -> QualityAssessment:
        content_chars = len(result.content.strip())
        if result.quality_score is not None:
            score = result.quality_score
        elif self._min_content_chars == 0:
            score = 1.0
        else:
            score = min(content_chars / self._min_content_chars, 1.0)
        accepted = content_chars >= self._min_content_chars and score >= minimum_score
        message = None
        if content_chars < self._min_content_chars:
            message = f"extracted {content_chars} characters; requires {self._min_content_chars}"
        elif score < minimum_score:
            message = f"provider quality {score:.2f} is below required quality {minimum_score:.2f}"
        return QualityAssessment(score=score, accepted=accepted, message=message)
