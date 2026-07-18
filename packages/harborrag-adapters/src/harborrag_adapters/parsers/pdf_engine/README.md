# PDF Engine

`pdf_engine` contains the pluggable PDF backend stack used by `PdfParser`.
The top-level parser owns routing and fallback behavior; each backend owns one
third-party extraction engine and returns a normalized `PdfParseResult`.

## Contract

Every backend implements:

```python
class PdfBackend:
    name: ClassVar[str]

    def parse(self, input: ParseInput) -> PdfParseResult:
        ...
```

`PdfParseResult` carries:

- `content`: canonical extracted text for indexing.
- `elements`: optional page-level or structure-level `DocumentElement`s.
- `metadata`: stable provenance such as page count, engine name, and options.
- `warnings`: non-fatal backend warnings.
- `raw`: optional backend-native data, only when explicitly requested.

Backends should raise:

- `ImportError` when the optional dependency is not installed.
- `ParseError` when the dependency exists but cannot parse the input.

`PdfParser` catches both and continues through the configured backend chain.

## Profiles

Profiles are backend order presets:

| Profile | Goal |
| --- | --- |
| `fast` | Prefer quick local text extraction. |
| `balanced` | Default. Try fast extraction, layout-aware engines, and OCR fallbacks. |
| `ocr` | Prefer scanned-document and full-page OCR behavior. |
| `quality` | Prefer richer layout, table, formula, and document-analysis engines. |

```python
from harborrag_adapters.parsers.pdf_engine import PdfParser, PdfParserProfile

parser = PdfParser(profile=PdfParserProfile.QUALITY, min_content_chars=50)
document = parser.parse("report.pdf")
```

Custom backend order is supported:

```python
from harborrag_adapters.parsers.pdf_engine import PdfParser, PyMuPdfBackend

parser = PdfParser(backends=[PyMuPdfBackend()], min_content_chars=10)
```

## Backends

| Backend | Best For | Dependency |
| --- | --- | --- |
| `PyMuPdfBackend` | Fast embedded-text extraction. | `PyMuPDF` |
| `DoclingBackend` | Layout-aware extraction, tables, OCR, structured export. | `docling` |
| `LiteParseBackend` | LlamaIndex LiteParse local parsing with page output. | `liteparse` |
| `MinerUBackend` | MinerU CLI pipelines and generated Markdown/JSON artifacts. | `mineru` CLI |
| `PaddleOcrBackend` | OCR-heavy document analysis and modern PaddleOCR pipelines. | `paddleocr` |

Install advanced dependencies with:

```bash
pip install -e "packages/harborrag-adapters[pdf]"
```

Some engines also require system packages, model downloads, GPU runtimes, or
CLI setup. Keep those checks inside the backend so the default registry can
still import when optional packages are absent.

## Design Rules

- Keep each third-party integration in its own backend file.
- Keep backend-specific helpers inside the backend class.
- Put shared PDF helpers in `pdf_engine/utils.py`.
- Do not import optional PDF dependencies at module import time.
- Avoid shell interpolation. CLI backends must pass command arguments as a list.
- Use `materialized_pdf_path()` for libraries that require a filesystem path.
- Return `content` as the canonical text field. Do not duplicate it elsewhere.
- Include raw backend output only behind an explicit option.

## Adding A Backend

1. Add `<engine>.py` with a `PdfBackend` implementation.
2. Add an options dataclass if configuration is more than one or two values.
3. Normalize third-party output into `PdfParseResult`.
4. Add the backend to `pdf_engine/__init__.py`.
5. Add it to `PdfParser.default_backends()` only when it is safe as an optional
   dependency and has clear fallback behavior.
6. Add parser tests with fake backend objects before adding heavy dependency
   tests.

## Huge Files

This package enforces source-level size limits before parsing and supports
path-based backend calls. Global concurrency, resumable ingestion, backpressure,
and future stream or spooled-file contracts belong in `harborrag-runtime`.
