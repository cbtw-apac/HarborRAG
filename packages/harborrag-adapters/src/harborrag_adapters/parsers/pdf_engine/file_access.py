from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from harborrag_core.domain.parser import ParseInput

from ..exceptions import EncryptedPdfError
from ..input_loading import read_parse_input_bytes


@contextmanager
def materialized_pdf_path(value: ParseInput) -> Generator[Path, None, None]:
    """Yield a safe filesystem path for PDF engines that require one."""
    if value.path is not None:
        yield Path(value.path)
        return
    with TemporaryDirectory(prefix="harborrag-pdf-") as directory:
        path = Path(directory) / "document.pdf"
        path.write_bytes(read_parse_input_bytes(value))
        yield path


def raise_if_encrypted_pdf(path: Path) -> None:
    """Fail fast on password-protected PDFs when PyMuPDF is available."""
    try:
        import pymupdf as pdf_module
    except ImportError:
        try:
            import fitz as pdf_module  # type: ignore[no-redef]
        except ImportError:
            return
    try:
        document = pdf_module.open(str(path), filetype="pdf")
    except Exception:  # noqa: BLE001 - best-effort pre-check boundary
        return
    try:
        if getattr(document, "needs_pass", False):
            raise EncryptedPdfError("PDF is password-protected")
    finally:
        document.close()
