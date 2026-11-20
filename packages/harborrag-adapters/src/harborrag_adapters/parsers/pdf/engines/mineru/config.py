"""MinerU provider configuration."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class MinerUPDFConfig:
    """Configuration for invoking the MinerU command-line parser.

    A command-line option left as None is not passed to MinerU, which keeps
    its own default.

    Attributes:
        backend: MinerU parsing backend, such as ``pipeline``. Non-empty text.
        executable: Name or path of the MinerU command, looked up on ``PATH``.
        timeout_seconds: Seconds one run may take before it is stopped,
            greater than 0.
        method: MinerU parsing method, one of ``auto``, ``txt`` or ``ocr``.
        language: Language passed to MinerU as ``--lang``.
        effort: MinerU effort level, ``medium`` or ``high``.
        api_url: Address passed to MinerU as ``--api-url``.
        server_url: Address passed to MinerU as ``--url``.
        output_dir: Directory that receives one subdirectory per run. None
            uses a temporary directory unless ``keep_output`` is set.
        keep_output: Keep a run's output directory instead of removing it.
            Without ``output_dir`` it goes under ``.harborrag-mineru-output``
            in the current directory.
        extra_args: Further command-line arguments, appended as written.
        env: Variables added to the environment of the MinerU process.
        working_directory: Directory the MinerU process runs in.
        max_output_files: Most Markdown or JSON output files a run may produce.
        max_output_bytes: Most bytes a run's output files may hold in total.
        max_diagnostic_bytes: Most bytes of MinerU's diagnostic output quoted
            in an error.
    """

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
        """Validate CLI controls before any subprocess is started.

        Raises:
            ValueError: The backend is not non-empty text, or the timeout, the
                method, the effort or an output limit is outside what it permits.
        """
        if not isinstance(self.backend, str) or not self.backend.strip():
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


MinerUBackendOptions = MinerUPDFConfig
