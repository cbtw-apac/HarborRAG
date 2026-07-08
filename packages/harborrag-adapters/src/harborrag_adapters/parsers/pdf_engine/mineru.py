from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar

from harborrag_core.domain.parser import ParseInput

from ..exceptions import ParseError
from ..utils import compact_text
from .base import PdfBackend, PdfParseResult
from .utils import (
    content_element,
    content_from_any,
    materialized_pdf_path,
    merge_dataclass_options,
)


@dataclass(slots=True)
class MinerUBackendOptions:
    """Configuration for invoking the MinerU command-line parser."""

    backend: str = "pipeline"
    executable: str = "mineru"
    timeout_seconds: int = 600
    effort: str | None = None
    api_url: str | None = None
    output_dir: Path | str | None = None
    keep_output: bool = False
    extra_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    working_directory: Path | str | None = None


class MinerUBackend(PdfBackend):
    """PDF backend that shells out to the MinerU CLI."""

    name: ClassVar[str] = "mineru"

    def __init__(
        self,
        options: MinerUBackendOptions | None = None,
        **overrides: Any,
    ) -> None:
        """Create a MinerU backend from options plus keyword overrides."""

        self.options = merge_dataclass_options(
            options,
            MinerUBackendOptions,
            overrides,
        )

    def parse(self, input: ParseInput) -> PdfParseResult:
        """Run MinerU against a PDF path and read generated Markdown or JSON output."""

        executable = shutil.which(self.options.executable)
        if executable is None:
            raise ImportError(
                "PDF parsing with MinerU requires the `mineru` CLI; install "
                "`mineru[all]` and ensure `mineru` is on PATH."
            )

        with materialized_pdf_path(input) as path:
            content, output_files, output_dir = self._parse_path(executable, path)

        return PdfParseResult(
            content=content,
            engine=self.name,
            elements=content_element(self.name, content),
            metadata={
                "source_engine": self.name,
                "mineru_backend": self.options.backend,
                "mineru_effort": self.options.effort,
                "output_files": output_files,
                "output_dir": str(output_dir)
                if self.options.output_dir or self.options.keep_output
                else None,
            },
        )

    def _parse_path(self, executable: str, path: Path) -> tuple[str, list[str], Path]:
        """Run MinerU using either a persistent or temporary output directory."""

        if self.options.output_dir is not None:
            output_dir = Path(self.options.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            command = self._command(executable, path, output_dir)
            self._run(command)
            content, output_files = self._read_output(output_dir)
            return content, output_files, output_dir

        if self.options.keep_output:
            output_dir = Path.cwd() / ".harborrag-mineru-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            command = self._command(executable, path, output_dir)
            self._run(command)
            content, output_files = self._read_output(output_dir)
            return content, output_files, output_dir

        with TemporaryDirectory(prefix="harborrag-mineru-") as directory:
            output_dir = Path(directory)
            command = self._command(executable, path, output_dir)
            self._run(command)
            content, output_files = self._read_output(output_dir)
            return content, output_files, output_dir

    def _command(self, executable: str, input_path: Path, output_dir: Path) -> list[str]:
        """Build the MinerU CLI command without shell interpolation."""

        command = [
            executable,
            "-p",
            str(input_path),
            "-o",
            str(output_dir),
            "-b",
            self.options.backend,
        ]
        if self.options.effort:
            command.extend(["--effort", self.options.effort])
        if self.options.api_url:
            command.extend(["--api-url", self.options.api_url])
        command.extend(self.options.extra_args)
        return command

    def _run(self, command: list[str]) -> None:
        """Execute MinerU with timeout, cwd, and environment overrides."""

        env = os.environ.copy()
        env.update(self.options.env)
        cwd = (
            Path(self.options.working_directory)
            if self.options.working_directory is not None
            else None
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                cwd=cwd,
                env=env,
                text=True,
                timeout=self.options.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ParseError(
                f"MinerU timed out after {self.options.timeout_seconds} seconds."
            ) from exc

        if completed.returncode != 0:
            detail = compact_text(completed.stderr or completed.stdout)
            raise ParseError(
                f"MinerU failed with exit code {completed.returncode}: {detail}"
            )

    @staticmethod
    def _read_output(output_dir: Path) -> tuple[str, list[str]]:
        """Read MinerU Markdown first, then fall back to JSON outputs."""

        markdown_files = sorted(output_dir.rglob("*.md"))
        output_files = [str(path.relative_to(output_dir)) for path in markdown_files]
        if markdown_files:
            sections = [
                path.read_text(encoding="utf-8", errors="replace")
                for path in markdown_files
            ]
            return compact_text("\n\n".join(sections)), output_files

        json_files = sorted(output_dir.rglob("*.json"))
        output_files = [str(path.relative_to(output_dir)) for path in json_files]
        sections = []
        for path in json_files:
            text = path.read_text(encoding="utf-8", errors="replace")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = text
            sections.append(content_from_any(payload))
        return compact_text("\n\n".join(sections)), output_files
