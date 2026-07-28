"""DOCX engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DocxEngineConfig:
    """Reserved DOCX provider settings."""
