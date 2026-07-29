"""MinerU provider configuration."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class MinerUPDFConfig:
    """Configuration for invoking the MinerU command-line parser."""

    backend: str = "pipeline"
    executable: str = "mineru"
    timeout_seconds: int = 600
    method: str | None = None
    language: str | None = None
    effort: str | None = None
    api_url: str | None = None
    server_url: str | None = None
    output_dir: Path | str | None = None
    keep_output: bool = False
    extra_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    working_directory: Path | str | None = None
    max_output_files: int = 1_000
    max_output_bytes: int = 256 * 1024 * 1024
    max_diagnostic_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        """Validate CLI controls before any subprocess is started."""

        if not self.backend.strip():
            raise ValueError("MinerU backend must be a non-empty string")
        if self.timeout_seconds <= 0:
            raise ValueError("MinerU timeout_seconds must be greater than 0")
        if self.method not in {None, "auto", "txt", "ocr"}:
            raise ValueError("MinerU method must be one of: auto, txt, ocr")
        if self.effort not in {None, "medium", "high"}:
            raise ValueError("MinerU effort must be one of: medium, high")
        if (
            min(
                self.max_output_files,
                self.max_output_bytes,
                self.max_diagnostic_bytes,
            )
            <= 0
        ):
            raise ValueError("MinerU output and diagnostic limits must be positive")


__all__ = ["MinerUPDFConfig"]
