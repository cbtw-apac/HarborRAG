# PDF parser family

The PDF family owns the full parsing workflow:

```text
validate and materialize input
    -> resolve profile or explicit engine
    -> run provider
    -> evaluate PDF quality
    -> continue on unavailable, failed, or low-quality output
    -> normalize the accepted output
```

The responsibilities are split across:

- `base.py`: `HarborPDFEngine`, the provider contract.
- `config.py`: named parsing profiles and router configuration.
- `parser.py`: orchestration, fallback, attempt tracking, and logging.
- `router.py`: `PDFEngineRegistry` and `PDFEngineRouter`.
- `quality.py`: PDF-specific acceptance policy.
- `normalization.py`: provider output to common `ParseResult`.
- `engines/<provider>/`: one independent external integration.

## Engine contract

Every provider supplies a stable name and returns `PDFParseResult`.

```python
class ExamplePDFEngine(HarborPDFEngine):
    @property
    def name(self) -> str:
        return "example"

    def parse_input(self, input: ParseInput) -> PDFParseResult: ...
```

Capability properties such as `supports_ocr`, `supports_tables`, and
`supports_layout` describe the provider without leaking those concepts into
the global parser contract.

Provider modules raise `ImportError` when an optional dependency is absent and
`ParseError` when an installed provider cannot parse the input. The family
parser records the attempt and continues to the next configured engine.
Encrypted PDFs fail distinctly because the remaining engines cannot extract
their content either.

## Profiles

Built-in profiles cover `fast`, `balanced`, `ocr`, `ocr_first`, `quality`,
and `scientific`. A `PDFRouterConfig` may define application-specific profiles:

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

An explicit `ParseRequest.engine` bypasses the profile order and selects that
registered provider.

## Providers

| Provider | Primary use | Optional extra |
| --- | --- | --- |
| PyMuPDF | Fast embedded-text extraction | `pdf-pymupdf` |
| Docling | Layout, tables, structured export, and OCR | `pdf-docling` |
| LiteParse | Local parsing with page-oriented output | `pdf-liteparse` |
| MinerU | CLI pipelines and generated Markdown/JSON | `pdf-mineru` |
| PaddleOCR | OCR-heavy document analysis | `pdf-ocr` |

Each provider keeps configuration in its own `config.py`. Optional imports and
model initialization remain inside the provider so importing the PDF family
does not require every dependency.

## Adding a provider

1. Add `engines/<provider>/config.py`, `engine.py`, and mapping helpers when
   required.
2. Implement `HarborPDFEngine` without importing another provider.
3. Normalize the provider response into `PDFParseResult`.
4. Inject the provider into `PDFEngineRegistry` or add it to the default
   construction policy.
5. Add lightweight fake-provider tests for routing and fallback before tests
   that require the external dependency.

CLI engines pass arguments as a list without shell interpolation. Engines that
require a path use `materialized_pdf_path()`, and raw provider output is
returned only behind an explicit option.
