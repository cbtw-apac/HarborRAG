from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar

from harborrag_adapters.parsers.common.normalization import compact_text
from harborrag_adapters.parsers.errors import ParseError
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_adapters.parsers.pdf.engines.mineru.config import MinerUPDFConfig
from harborrag_adapters.parsers.pdf.models import PDFParseResult
from harborrag_adapters.parsers.pdf.normalization import (
    content_element,
    content_from_any,
)
from harborrag_adapters.parsers.pdf.resources import materialized_pdf_path
from harborrag_adapters.parsers.pdf.utils import merge_dataclass_options
from harborrag_core.domain.parser import ParseInput

MinerUBackendOptions = MinerUPDFConfig


class MinerUPDFEngine(HarborPDFEngine):
    """PDF backend that shells out to the MinerU CLI."""

    name: ClassVar[str] = "mineru"

    def __init__(
        self,
        options: MinerUPDFConfig | None = None,
        **overrides: Any,
    ) -> None:
        """Create a MinerU backend from options plus keyword overrides."""

        self.options = merge_dataclass_options(
            options,
            MinerUPDFConfig,
            overrides,
        )

    @property
    def supports_ocr(self) -> bool:
        return True

    @property
    def supports_tables(self) -> bool:
        return True

    @property
    def supports_layout(self) -> bool:
        return True

    def parse_input(self, input: ParseInput) -> PDFParseResult:
        """Run MinerU against a PDF path and read generated Markdown or JSON output."""

        executable = shutil.which(self.options.executable)
        if executable is None:
            raise ImportError(
                "PDF parsing with MinerU requires the `mineru` CLI; install "
                "`mineru` and ensure `mineru` is on PATH."
            )

        with materialized_pdf_path(input) as path:
            content, output_files, output_dir = self._parse_path(executable, path)

        return PDFParseResult(
            content=content,
            engine=self.name,
            elements=content_element(self.name, content),
            metadata={
                "source_engine": self.name,
                "mineru_backend": self.options.backend,
                "mineru_effort": self.options.effort,
                "output_files": output_files,
                # Only report a directory that actually still exists on disk:
                # a custom `output_dir` without `keep_output=True` is cleaned
                # up after `_read_output` runs (see `_parse_path`), so
                # advertising its path here would point callers at a
                # directory that no longer exists.
                "output_dir": str(output_dir) if self.options.keep_output else None,
            },
        )

    def _parse_path(self, executable: str, path: Path) -> tuple[str, list[str], Path]:
        """Run MinerU using either a persistent or temporary output directory."""

        # Persistent output dirs MUST use a fresh per-document subdirectory:
        # reading a shared, accumulating directory with rglob() would silently
        # concatenate earlier documents' Markdown into this document's result
        # (cross-document / cross-tenant content contamination) and grow without
        # bound.
        if self.options.output_dir is not None:
            base_dir = Path(self.options.output_dir)
            output_dir = base_dir / uuid.uuid4().hex
            output_dir.mkdir(parents=True, exist_ok=True)
            try:
                command = self._command(executable, path, output_dir)
                self._run(command)
                content, output_files = self._read_output(output_dir)
            finally:
                # `output_dir` only means "use this custom location", not
                # "retain output forever" -- that's what `keep_output` is
                # for. Without this, every parsed document would leave
                # behind an uncleaned per-document subdirectory under the
                # user-supplied output_dir.
                if not self.options.keep_output:
                    shutil.rmtree(output_dir, ignore_errors=True)
            return content, output_files, output_dir

        if self.options.keep_output:
            base_dir = Path(self.options.output_dir or Path.cwd() / ".harborrag-mineru-output")
            output_dir = base_dir / uuid.uuid4().hex
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
        if self.options.method:
            command.extend(["--method", self.options.method])
        if self.options.language:
            command.extend(["--lang", self.options.language])
        if self.options.api_url:
            command.extend(["--api-url", self.options.api_url])
        if self.options.server_url:
            command.extend(["--url", self.options.server_url])
        command.extend(self.options.extra_args)
        return command

    def _run(self, command: list[str]) -> None:
        """Execute MinerU with timeout, cwd, and environment overrides."""

        env = {
            name: value
            for name, value in os.environ.items()
            if name
            in {
                "PATH",
                "LANG",
                "LC_ALL",
                "TMPDIR",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "REQUESTS_CA_BUNDLE",
                "CUDA_VISIBLE_DEVICES",
                "HF_HOME",
                "TORCH_HOME",
                "XDG_CACHE_HOME",
            }
        }
        env.update(self.options.env)
        cwd = (
            Path(self.options.working_directory)
            if self.options.working_directory is not None
            else None
        )
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                cwd=cwd,
                env=env,
                start_new_session=(
                    os.name == "posix" and os.environ.get("HARBORRAG_ISOLATED_PROCESS") != "1"
                ),
            )
            try:
                return_code = process.wait(timeout=self.options.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                self._terminate_process_group(process)
                raise ParseError(
                    f"MinerU timed out after {self.options.timeout_seconds} seconds."
                ) from exc
            except BaseException:
                self._terminate_process_group(process)
                raise
            if return_code != 0:
                detail = self._diagnostic_text(stderr, stdout)
                raise ParseError(f"MinerU failed with exit code {return_code}: {detail}")

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        """Terminate MinerU and descendants, then reap the direct child."""

        if process.poll() is not None:
            return
        if os.name == "posix" and os.environ.get("HARBORRAG_ISOLATED_PROCESS") != "1":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()

    def _diagnostic_text(self, *streams: Any) -> str:
        for stream in streams:
            stream.seek(0)
            content = stream.read(self.options.max_diagnostic_bytes + 1)
            if content:
                if len(content) > self.options.max_diagnostic_bytes:
                    content = content[: self.options.max_diagnostic_bytes] + b"..."
                return compact_text(content.decode("utf-8", errors="replace"))
        return "no diagnostic output"

    def _read_output(self, output_dir: Path) -> tuple[str, list[str]]:
        """Read MinerU Markdown first, then fall back to JSON outputs."""

        markdown_files = self._safe_output_files(output_dir, "*.md")
        output_files = [str(path.relative_to(output_dir)) for path in markdown_files]
        if markdown_files:
            sections = self._read_capped_outputs(markdown_files)
            return compact_text("\n\n".join(sections)), output_files

        json_files = self._safe_output_files(output_dir, "*.json")
        output_files = [str(path.relative_to(output_dir)) for path in json_files]
        sections = []
        for text in self._read_capped_outputs(json_files):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = text
            sections.append(content_from_any(payload))
        return compact_text("\n\n".join(sections)), output_files

    def _safe_output_files(self, output_dir: Path, pattern: str) -> list[Path]:
        files: list[Path] = []
        for path in sorted(output_dir.rglob(pattern)):
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ParseError("MinerU produced a non-regular output file")
            files.append(path)
            if len(files) > self.options.max_output_files:
                raise ParseError("MinerU produced too many output files")
        return files

    def _read_capped_outputs(self, files: list[Path]) -> list[str]:
        remaining = self.options.max_output_bytes
        output: list[str] = []
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        for path in files:
            descriptor = os.open(path, flags)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > remaining:
                    raise ParseError("MinerU output exceeds the configured byte limit")
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    content = handle.read(remaining + 1)
                if len(content) > remaining:
                    raise ParseError("MinerU output exceeds the configured byte limit")
                remaining -= len(content)
                output.append(content.decode("utf-8", errors="replace"))
            finally:
                os.close(descriptor)
        return output


MinerUBackend = MinerUPDFEngine
