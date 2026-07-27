from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]

for source_path in (
    REPO_ROOT / "packages" / "harborrag-adapters" / "src",
    REPO_ROOT / "packages" / "harborrag-core" / "src",
):
    source = str(source_path)
    if source not in sys.path:
        sys.path.insert(0, source)

from harborrag_adapters.parsers import HarborParserFactory  # noqa: E402
from harborrag_adapters.parsers.compat import (  # noqa: E402
    DoclingBackend,
    LiteParseBackend,
    MinerUBackend,
    PaddleOcrBackend,
    PdfParser,
    PdfParserProfile,
    PyMuPdfBackend,
)
from harborrag_core.domain.parser import ParseInput  # noqa: E402

PDF_BACKENDS = {
    "docling": DoclingBackend,
    "liteparse": LiteParseBackend,
    "mineru": MinerUBackend,
    "paddleocr": PaddleOcrBackend,
    "pymupdf": PyMuPdfBackend,
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse one real local document without pytest or test doubles."
    )
    parser.add_argument("path", type=Path, help="Real document to parse")
    pdf_selection = parser.add_mutually_exclusive_group()
    pdf_selection.add_argument(
        "--pdf-profile",
        choices=[profile.value for profile in PdfParserProfile],
        default=None,
        help="Run a specific PDF backend profile instead of automatic routing",
    )
    pdf_selection.add_argument(
        "--pdf-backend",
        choices=sorted(PDF_BACKENDS),
        default=None,
        help="Run exactly one PDF backend instead of automatic routing",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    path = args.path.expanduser().resolve()
    if not path.is_file():
        print(f"[parsers] not configured: real input file does not exist: {path}")
        return 2
    if (args.pdf_profile or args.pdf_backend) and path.suffix.casefold() != ".pdf":
        print("[parsers] failed: PDF selection can only be used with a PDF file")
        return 1

    try:
        parse_input = ParseInput(path=path)
        if args.pdf_backend:
            backend = PDF_BACKENDS[args.pdf_backend]()
            document = PdfParser(backends=[backend]).parse_input(parse_input)
        elif args.pdf_profile:
            document = PdfParser(profile=args.pdf_profile).parse_input(parse_input)
        else:
            document = HarborParserFactory().create_registry().parse_input(parse_input)
        if not document.content.strip():
            raise AssertionError("parser returned empty extracted content")
    except Exception as exc:  # noqa: BLE001 - smoke runner returns a stable exit code
        detail = str(exc).replace("\r", " ").replace("\n", " ")[:500]
        print(f"[parsers] failed: {type(exc).__name__}: {detail}")
        return 1

    print(
        "[parsers] passed "
        f"parser={document.parser_name!r} chars={len(document.content)} "
        f"elements={len(document.elements or [])} warnings={len(document.warnings or [])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
