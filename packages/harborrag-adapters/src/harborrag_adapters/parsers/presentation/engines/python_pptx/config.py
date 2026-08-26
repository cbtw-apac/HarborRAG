"""python-pptx engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PythonPptxEngineConfig:
    """Presentation extraction controls."""

    include_notes: bool = True
