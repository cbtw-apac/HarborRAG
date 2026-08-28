# Parser families

Parsers are organized by document family. The root registry answers which
family owns an input; a family parser owns its engines, routing policy,
quality checks, fallback, and normalization.

```text
parsers/
├── registry.py              # MIME/extension -> parser family
├── factory.py               # dependency construction
├── common/                  # cross-family contracts and mechanics
├── pdf/                     # PDF workflow and provider engines
├── document/                # DOCX, ODT, and EPUB
├── spreadsheet/             # Excel and delimited data
├── presentation/            # PowerPoint
├── markup/                  # HTML and Markdown
├── structured/              # JSON, JSONL, and NDJSON
├── text/                    # plain text and source/config files
└── image/                   # image OCR
```

The architectural boundary is:

```text
HarborParserRegistry
    -> HarborPDFParser
        -> PDFEngineRouter
            -> DoclingPDFEngine
            -> MinerUPDFEngine
            -> PaddleOCRPDFEngine
            -> PyMuPDFEngine
```

Provider engines are never MIME or extension routes in the root registry.
They remain internal to their family and do not import one another.

## Public API

`HarborParser` (in `harborrag_adapters.parsers.common.base`, deliberately not exported
from `harborrag_adapters.parsers`) is the global family contract. New integrations submit a
`ParseRequest` and receive the same `ParseResult` shape for every family.

```python
from harborrag_adapters.parsers import HarborParserFactory, ParseRequest

registry = HarborParserFactory().create_registry()
parser = registry.resolve(
    filename="technical-report.pdf",
    mime_type="application/pdf",
)

result = await parser.parse(
    ParseRequest(
        source_uri="file:///data/technical-report.pdf",
        filename="technical-report.pdf",
        mime_type="application/pdf",
    )
)
```

`ParseResult` contains normalized elements and text, the family and selected
engine names, warnings, metadata, and an ordered provider-attempt history.
Current ingestion callers can continue using the synchronous
`registry.parse(ParseInput(...))` compatibility boundary while they migrate.
Older concrete class names are available explicitly from
`harborrag_adapters.parsers.compat`; they are not part of the root public API.

## Routing

The registry indexes only complete family parsers.

- MIME type wins when MIME and extension resolve to the same family.
- A known extension wins over a generic transport type such as
  `application/octet-stream`.
- A conflict between two specific family routes fails instead of guessing.
- `register_extension()` and `register_mime_type()` accept builders when an
  application needs custom dependency construction.
- `HarborParserFactory` registers the default families from `ParserConfig`.

Family engines use a second routing layer. Most current families select one
engine by format. PDF uses a named, configuration-driven profile and may try
several engines until its quality policy accepts a result.

See [Parser Configuration](../../../../../docs/users/configuration/parser-config.md)
for application YAML configuration.

## Optional dependencies

Install all common parser integrations:

```bash
pip install -e "packages/harborrag-adapters[parsers-all]"
```

Smaller extras are available for `document`, `spreadsheet`, `presentation`,
`markup`, `image-tesseract`, `image-rapidocr`, `pdf-pymupdf`, `pdf-docling`,
`pdf-liteparse`, `pdf-mineru`, and `pdf-ocr`. Provider imports remain lazy so
a family can be constructed when an unused optional dependency is absent.

## Adding a family or engine

A new family implements `HarborParser` in `<family>/parser.py`, declares its
supported MIME types and extensions, and defines its provider contract in
`<family>/base.py`. The factory then registers that family.

A new provider belongs under `<family>/engines/<provider>/`:

- `config.py` owns provider settings.
- `engine.py` integrates only that provider.
- `mapping.py` owns provider-schema conversion when needed.
- the family router receives the engine through dependency injection.

Only move behavior into `common/` after at least two families use it. PDF page
analysis, OCR policy, spreadsheet cells, presentation notes, and DOM cleanup
remain family-specific.

## Logging and tests

Parser logs use the `harborrag.adapters.parsers` namespace. Family parsers log
engine selection and failures without logging extracted document text.

Tests live under `packages/harborrag-adapters/tests/parsers/`, including
architecture, unit, failure, security, performance, and smoke coverage.
