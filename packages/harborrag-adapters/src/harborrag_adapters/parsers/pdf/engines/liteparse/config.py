"""LiteParse provider configuration."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class LiteParsePDFConfig:
    """Configuration for LlamaIndex LiteParse PDF extraction."""

    output_format: str = "markdown"
    image_mode: str = "placeholder"
    extract_links: bool = True
    ocr_enabled: bool = True
    ocr_language: str = "eng"
    ocr_server_url: str | None = None
    tessdata_path: Path | str | None = None
    max_pages: int | None = None
    target_pages: str | None = None
    dpi: int | float | None = None
    preserve_very_small_text: bool | None = None
    password: str | None = None
    quiet: bool | None = None
    num_workers: int | None = None
    input_mode: str = "path"
    include_raw: bool = False
    # Markdown output fences blocks LiteParse reads as preformatted, which on
    # scanned or single-column pages is plain prose. Not a LiteParse
    # constructor argument: applied to its output.
    strip_code_fences: bool = True
    parser: Any | None = None
    extra_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Treat a blank OCR server URL as unset, so LiteParse OCRs locally."""

        if isinstance(self.ocr_server_url, str):
            self.ocr_server_url = self.ocr_server_url.strip() or None


LiteParseBackendOptions = LiteParsePDFConfig

__all__ = ["LiteParseBackendOptions", "LiteParsePDFConfig"]
