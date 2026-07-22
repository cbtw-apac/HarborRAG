# Parsers

## File Configuration

Applications can select parser profiles and explicit PDF backend chains in
`config/parsers.yaml`, then load them through `harborrag_runtime.config`.
Runtime configuration constructs the parser adapters; parser implementations
remain independent from YAML parsing.

See [Parser Configuration](../../../../../docs/users/configuration/parser-config.md)
for the schema and registry replacement API.

Parsers convert raw source payloads into `ParsedDocument` objects for later RAG
stages. Shared parser schemas live in `harborrag-core`; this package owns the
format-specific extraction engines.

## Contract

Parser input and output:

- `ParseInput` contains the original payload plus routing metadata:
  `path`, `content`, `filename`, `content_type`, and `metadata`.
- `ParsedDocument` contains extracted `content`, optional structured
  `elements`, parser provenance, warnings, metadata, and optional raw backend
  data.

Every concrete parser extends `BaseParser` and declares:

- `parser_name`
- `parser_engine`
- `suffixes`
- `content_types`
- `parse(input)`

## HarborParser

`HarborParser` is the parser registry and factory.

```python
from harborrag_adapters.parsers import HarborParser, ParseInput

parser = HarborParser()

document = parser.parse(
    ParseInput(
        content=b"# Hello",
        filename="README.md",
        content_type="text/markdown",
    )
)

print(document.parser_name)
print(document.content)
```

Routing is deterministic:

- Suffix and MIME content type are indexed at registration time.
- A direct suffix route or content-type route is used when only one matches.
- A specific suffix wins when the transport MIME type is generic, such as
  `text/plain` or `application/octet-stream`.
- A content-type route is used when the input has no suffix.
- Other conflicts where suffix and content type match different parsers fail
  with `UnsupportedFormatError` instead of guessing.
- Route overrides require `replace=True`.

```python
parser = HarborParser()
markdown = parser.create("markdown")

parser.unregister("markdown")
parser.register(markdown, replace=True)
```

## Default Parsers

| Parser | Suffixes | Engine |
| --- | --- | --- |
| `PptxParser` | `.pptx`, `.pptm` | `python-pptx` |
| `DocxParser` | `.docx` | `docx2txt` |
| `ExcelParser` | `.xls`, `.xlsx`, `.xlsm`, `.xltx`, `.xltm` | `openpyxl`, `xlrd` |
| `PdfParser` | `.pdf` | PyMuPDF, Docling, LiteParse, MinerU, PaddleOCR |
| `CsvParser` | `.csv`, `.tsv` | Python `csv` |
| `ImageParser` | `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`, `.gif`, `.webp` | RapidOCR or `pytesseract`, Pillow |
| `HtmlParser` | `.html`, `.htm`, `.xhtml` | Beautiful Soup or stdlib HTML fallback |
| `EpubParser` | `.epub` | Python `zipfile` and XML parsing |
| `JsonParser` | `.json`, `.jsonl`, `.ndjson` | Python `json` |
| `MarkdownParser` | `.md`, `.markdown`, `.mdx` | Python regex text extraction |
| `TextParser` | Plain text and common source/config suffixes | Python text decoding |

Install common parser dependencies:

```bash
pip install -e "packages/harborrag-adapters[parsers]"
```

Install advanced PDF dependencies, including RapidOCR and its ONNX runtime:

```bash
pip install -e "packages/harborrag-adapters[pdf]"
```

## PDF Backends

`PdfParser` tries backends in profile order and returns the first backend that
produces enough content. Missing optional backend packages are reported as
warnings while later backends are attempted.

Profiles:

| Profile | Goal |
| --- | --- |
| `fast` | Prefer quick text extraction. |
| `balanced` | Default. Try fast extraction, layout-aware engines, and OCR fallbacks. |
| `ocr` | Prefer OCR-heavy processing for scanned documents. |
| `quality` | Prefer richer layout, table, formula, and document-analysis engines. |

Example:

```python
from harborrag_adapters.parsers import PdfParser, PdfParserProfile

pdf_parser = PdfParser(profile=PdfParserProfile.QUALITY)
parsed = pdf_parser.parse("report.pdf")
```

Custom backend ordering:

```python
from harborrag_adapters.parsers import PdfParser, PyMuPdfBackend

pdf_parser = PdfParser(backends=[PyMuPdfBackend()], min_content_chars=10)
```

## Writing A Parser

Keep a parser self-contained:

```python
from typing import ClassVar

from harborrag_adapters.parsers import BaseParser, ParseInput, ParsedDocument


class MyParser(BaseParser[ParseInput, ParsedDocument]):
    parser_name: ClassVar[str] = "my_format"
    parser_engine: ClassVar[str] = "my-library"
    suffixes: ClassVar[frozenset[str]] = frozenset({"mine"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/x-mine"})

    def parse(self, input: ParseInput) -> ParsedDocument:
        parse_input = self.coerce_input(input)
        content = parse_input.read_text()
        return ParsedDocument(
            content=content,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input),
        )
```

Guidelines:

- Put helpers used only by one parser inside that parser class.
- Put helpers shared across parser files in `parsers/utils.py`.
- Keep optional dependency imports inside `parse()` or backend methods.
- Raise `ParseError` for expected dependency or malformed-input failures.
- Do not duplicate content fields. `ParsedDocument.content` is the canonical
  extracted text.
- Add elements only when they provide useful structure for chunking or metadata.

## Logging

Parser logging uses:

```text
harborrag.adapters.parsers
```

Use `get_parser_logger()` and `parser_log_extra()` so parser name, engine, route,
filename, and content type stay consistent across format implementations.
Prefer structured counters such as `input_bytes`, `content_chars`, `elements`,
`pages`, `slides`, `sheets`, and `rows`; never log extracted document text.

## Tests

Parser tests live in:

```text
packages/harborrag-adapters/tests/parsers/unit/
packages/harborrag-adapters/tests/parsers/failure/
packages/harborrag-adapters/tests/parsers/security/
packages/harborrag-adapters/tests/parsers/performance/
packages/harborrag-adapters/tests/parsers/smoke/
```

Useful test levels:

- Standalone smoke checks for real document extraction and optional PDF engines.
- Whitebox tests for route indexes and conflict behavior.
- Graybox tests for parser metadata and warnings.
- Blackbox tests that parse representative inputs by suffix and content type.
