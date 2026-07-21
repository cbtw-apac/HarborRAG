"""Minimal shared setup for standalone smoke scripts."""

from __future__ import annotations

import os
import reprlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]

for source_path in (
    REPO_ROOT / "packages" / "harborrag-adapters" / "src",
    REPO_ROOT / "packages" / "harborrag-core" / "src",
):
    source = str(source_path)
    if source not in sys.path:
        sys.path.insert(0, source)

from harborrag_core.security.redaction import redact_secrets  # noqa: E402


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env() -> Path:
    configured_path = os.getenv("HARBOR_SMOKE_ENV_FILE")
    env_path = Path(configured_path).expanduser() if configured_path else REPO_ROOT / ".env"
    if not env_path.exists():
        return env_path
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = _unquote(value)
    return env_path


def env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value else None


def env_path(name: str) -> Path | None:
    value = env(name)
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def attachment_parser():
    """Build the real attachment parser stack with an optional exact PDF backend."""
    from harborrag_adapters.parsers import (
        DoclingBackend,
        HarborParser,
        LiteParseBackend,
        MinerUBackend,
        PaddleOcrBackend,
        PdfParser,
        PyMuPdfBackend,
    )

    parser = HarborParser()
    backend_name = (env("HARBOR_SMOKE_PDF_BACKEND") or "").casefold()
    if not backend_name:
        return parser

    backend_factories = {
        "liteparse": LiteParseBackend,
        "mineru": MinerUBackend,
        "paddleocr": PaddleOcrBackend,
        "pymupdf": PyMuPdfBackend,
    }
    if backend_name == "docling":
        requested_device = env("HARBOR_SMOKE_DOCLING_DEVICE") or "auto"
        backend = DoclingBackend(accelerator_device=requested_device)
        resolved_device = backend.resolved_accelerator_device()
        backend.options.accelerator_device = resolved_device
        print(
            "[attachments] Docling accelerator "
            f"requested={requested_device!r} resolved={resolved_device!r}"
        )
    else:
        try:
            backend = backend_factories[backend_name]()
        except KeyError as exc:
            choices = ", ".join(sorted([*backend_factories, "docling"]))
            raise ValueError(
                f"Unsupported HARBOR_SMOKE_PDF_BACKEND {backend_name!r}; choose {choices}"
            ) from exc

    parser.register(PdfParser(backends=[backend]), replace=True)
    print(f"[attachments] PDF backend={backend_name!r}")
    if _image_backend_name():
        print("[attachments] image OCR backend='rapidocr'")
    return parser


def _image_backend_name() -> str:
    """Resolve image OCR, reusing Docling's RapidOCR runtime by default."""
    configured = env("HARBOR_SMOKE_IMAGE_BACKEND")
    if configured:
        return configured.casefold()
    if (env("HARBOR_SMOKE_PDF_BACKEND") or "").casefold() == "docling":
        return "rapidocr"
    return ""


def attachment_custom_parsers():
    """Return smoke-only attachment overrides selected by environment."""
    from harborrag_adapters.connectors.shared.attachments import FileType

    backend_name = _image_backend_name()
    if not backend_name:
        return {}
    if backend_name != "rapidocr":
        raise ValueError(
            f"Unsupported HARBOR_SMOKE_IMAGE_BACKEND {backend_name!r}; choose rapidocr"
        )
    return {FileType.IMAGE: _parse_image_with_rapidocr}


_RAPID_OCR_ENGINE: Any | None = None


def _rapidocr_engine():
    """Build one RapidOCR engine and reuse its loaded ONNX models."""
    global _RAPID_OCR_ENGINE
    if _RAPID_OCR_ENGINE is None:
        try:
            import onnxruntime
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise RuntimeError(
                "RapidOCR image parsing requires `harborrag-adapters[pdf]`, "
                "which installs both `rapidocr` and `onnxruntime`"
            ) from exc
        _RAPID_OCR_ENGINE = RapidOCR()
        providers = onnxruntime.get_available_providers()
        print(
            "[attachments] RapidOCR runtime='onnxruntime' "
            f"provider='CPUExecutionProvider' available_providers={providers!r}"
        )
    return _RAPID_OCR_ENGINE


def _parse_image_with_rapidocr(content: bytes, extension: str) -> str:
    """Extract ordered text lines from one image attachment with RapidOCR."""
    _ = extension
    result = _rapidocr_engine()(content)
    texts = getattr(result, "txts", None) or ()
    return "\n".join(str(text).strip() for text in texts if str(text).strip())


PREVIEW_CHARS = 200
_VERBOSE_VALUES = {"1", "true", "yes", "on"}


def _verbose_previews_enabled() -> bool:
    """Require an explicit local opt-in before showing provider content."""
    if os.getenv("CI"):
        return False
    return os.getenv("HARBOR_SMOKE_VERBOSE", "").strip().lower() in _VERBOSE_VALUES


def _preview(value: object, *, limit: int = PREVIEW_CHARS) -> str:
    """Render a bounded, redacted preview without formatting whole large values."""
    if isinstance(value, str):
        total = len(value)
        text = value[:limit]
    elif isinstance(value, bytes):
        total = len(value)
        text = repr(value[:limit])
    else:
        renderer = reprlib.Repr()
        renderer.maxstring = limit
        renderer.maxother = limit
        renderer.maxdict = 10
        renderer.maxlist = 10
        text = renderer.repr(value)
        total = len(text)

    text = redact_secrets(text)
    if total <= limit:
        return text
    return f"{text}… (truncated, {total} chars total)"


def print_document(provider: str, document) -> None:
    verbose = _verbose_previews_enabled()
    print(f"\n[{provider}] loaded document")
    print(f"[{provider}] id={document.id!r}")
    print(f"[{provider}] content_type={document.content_type!r}")
    text = document.text()
    print(f"[{provider}] chars={len(text)}")
    if verbose:
        print(f"[{provider}] source preview={_preview(document.source)!r}")
        print(f"[{provider}] content preview={_preview(text)!r}")
        print(f"[{provider}] metadata preview={_preview(document.metadata)!r}")
        if document.raw is not None:
            print(f"[{provider}] raw preview={_preview(document.raw)!r}")
    print_attachments(provider, document, verbose=verbose)


def print_attachments(provider: str, document, *, verbose: bool = False) -> None:
    attachments = (document.metadata or {}).get("attachments") or []
    if not attachments:
        print(f"[{provider}] attachments: none")
        return
    print(f"[{provider}] attachments: {len(attachments)}")
    for index, attachment in enumerate(attachments, start=1):
        title = attachment.get("title")
        status = attachment.get("status")
        size_bytes = attachment.get("size_bytes")
        text = attachment.get("text") or ""
        reason = attachment.get("reason")
        line = f"  - attachment={index} status={status!r} "
        line += f"size_bytes={size_bytes} text_chars={len(text)}"
        if verbose and title:
            line += f" title={_preview(title)!r}"
        if verbose and reason:
            line += f" reason={_preview(reason)!r}"
        print(line)
        if verbose and text:
            print(f"    preview={_preview(text)!r}")


def attachments_passed(provider: str, document) -> bool:
    """Require every attempted attachment to avoid an unsupported/failed state."""
    attachments = (document.metadata or {}).get("attachments") or []
    failures = [
        attachment
        for attachment in attachments
        if attachment.get("status") in {"failed", "unsupported"}
    ]
    if not failures:
        return True
    print(f"[{provider}] attachment smoke failed: {len(failures)} attachment(s) failed")
    return False


def print_failure(provider: str, exc: Exception) -> None:
    """Print a bounded, redacted smoke failure instead of a traceback."""
    detail = redact_secrets(str(exc)).replace("\r", " ").replace("\n", " ")[:500]
    print(f"[{provider}] failed: {type(exc).__name__}: {detail}")
