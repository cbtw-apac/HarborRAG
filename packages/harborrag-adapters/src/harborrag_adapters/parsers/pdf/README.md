# PDF parser family

The PDF family turns a PDF into text and elements by trying one or more PDF engines,
such as LiteParse, PyMuPDF or Docling, until one produces output good enough to keep.
This page is for contributors working in this folder: the parsing workflow, the engine
contract, profiles and how to add an engine. How users choose PDF engines in
`config/ingestion/parsers.yaml` is described in
[Parser configuration: PDFs](../../../../../../docs/users/configuration/parser-config.md#pdfs).

## The parsing workflow

`HarborPDFParser.parse` in `parser.py` runs these steps:

```text
empty input (0 bytes)          -> empty result, no engine runs
size and page-count guards     -> reject before any engine reads the file
resolve the profile, or the request's explicit engine
for each engine in order:
    run the engine
    evaluate PDF quality
    continue when the engine is unavailable, fails, or its output scores too low
normalize the first accepted output
```

An engine that raises `ImportError` because its optional dependency is missing is
recorded as unavailable. A page-limit, size-limit or no-extractable-text error is
recorded as a failure of that engine, because another engine, such as an OCR engine,
may still succeed. An encrypted PDF stops the chain at once, because no other engine can
read it either. Every attempt is recorded in the result's `attempts`. When no engine
succeeds, `raise_pdf_failure` in `parser_failures.py` re-raises the shared error if
every engine failed the same typed way, such as the page limit, and otherwise raises
`PDFParsingFailedError` carrying the attempts.

The limits themselves, such as 200 pages per PDF, are listed in
[Parser configuration: document limits](../../../../../../docs/users/configuration/parser-config.md#document-limits).

## Files

| File | What it holds |
| --- | --- |
| `base.py` | `HarborPDFEngine`, the engine contract |
| `config.py` | `PDFParserConfig`, `PDFRouterConfig` and the named profiles |
| `parser.py` | `HarborPDFParser`: orchestration, fallback, attempt tracking and logging |
| `parser_support.py` | Empty-input handling and the size and page-count guards |
| `page_limit.py` | Page counting for the page-count guard |
| `parser_failures.py` | Classification of engine failures and the final failure |
| `router.py` | `PDFEngineRegistry` and `PDFEngineRouter` |
| `quality.py` | The PDF quality policy that accepts or rejects an engine's output |
| `normalization.py` | Conversion of the accepted output into the common `ParseResult` |
| `models.py` | `PDFParseResult`, the result every engine returns |
| `resources.py` | `materialized_pdf_path()`, which gives a path-based engine a file on disk |
| `engines/<engine>/` | One external integration each |

## Engine contract

Every engine has a stable name and returns a `PDFParseResult`:

```python
class ExamplePDFEngine(HarborPDFEngine):
    @property
    def name(self) -> str:
        return "example"

    def parse_input(self, input: ParseInput) -> PDFParseResult: ...
```

The `supports_ocr`, `supports_tables` and `supports_layout` properties describe the
engine without adding those ideas to the global parser contract.

An engine raises `ImportError` when its optional dependency is absent and `ParseError`
when it is installed but cannot parse the input.

## Profiles

A profile is a named engine order with a minimum quality score. The built-in profiles,
with `liteparse` (LiteParse, then PyMuPDF) as the default, are listed in
[Parser configuration: other PDF engines](../../../../../../docs/users/configuration/parser-config.md#other-pdf-engines).
`PDFRouterConfig` can define an application's own profiles:

```python
from harborrag_adapters.parsers.pdf.config import (
    PDFParserConfig,
    PDFProfileConfig,
    PDFRouterConfig,
)

config = PDFParserConfig(
    router=PDFRouterConfig(
        default_profile="research",
        profiles={
            "research": PDFProfileConfig(
                engine_order=("docling", "mineru", "pymupdf"),
                minimum_quality_score=0.85,
                preserve_tables=True,
                preserve_layout=True,
            )
        },
    )
)
```

Passing `profiles` replaces the built-in set, so `default_profile` must name one of the
profiles you pass. `PDFParserConfig` also sets `min_content_chars` (default `20`) and
`max_pages` (default `200`).

A request whose `ParseRequest.engine` names a registered engine skips the profile order
and uses that engine.

## Engines

| Engine | Folder | Best for | Extra |
| --- | --- | --- | --- |
| LiteParse | `engines/liteparse/` | Local parsing with page-oriented output | `pdf-liteparse` |
| PyMuPDF | `engines/pymupdf/` | Fast extraction of embedded text | `pdf-pymupdf` |
| Docling | `engines/docling/` | Layout, tables, structured export and OCR | `pdf-docling` |
| MinerU | `engines/mineru/` | CLI pipelines with generated Markdown and JSON | `pdf-mineru` |
| PaddleOCR | `engines/paddleocr/` | OCR-heavy documents | `pdf-ocr` |

Each engine keeps its settings in its own `config.py`. Optional imports and model
loading stay inside the engine, so importing the PDF family needs none of these
dependencies.

## Add an engine

1. Add `engines/<engine>/config.py` and `engine.py`, plus `mapping.py` when the
   engine's output needs converting.
2. Implement `HarborPDFEngine` without importing another engine.
3. Convert the engine's output into a `PDFParseResult`.
4. Inject the engine into `PDFEngineRegistry`, or add it to the default construction
   policy.
5. Test routing and fallback with a lightweight fake engine before adding tests that
   need the real dependency.

An engine that runs a CLI, as MinerU does, passes its arguments as a list, never
through a shell. An engine that needs a file path gets one from
`materialized_pdf_path()`. Raw engine output is returned only when an option asks for
it, such as LiteParse's `include_raw` or Docling's `include_raw_document`, both off by
default.
