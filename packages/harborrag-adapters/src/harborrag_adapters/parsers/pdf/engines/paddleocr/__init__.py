"""PaddleOCR PDF provider engine."""

from harborrag_adapters.parsers.pdf.engines.paddleocr.config import (
    PaddleOCRPDFConfig,
)
from harborrag_adapters.parsers.pdf.engines.paddleocr.engine import PaddleOCRPDFEngine

__all__ = ["PaddleOCRPDFConfig", "PaddleOCRPDFEngine"]
