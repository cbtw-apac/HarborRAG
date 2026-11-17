# Parser families

A parser turns a file, such as a PDF or a spreadsheet, into text and structured
elements that HarborRAG can chunk. This page is for contributors working in this
folder: how parsers are organized into families, how a file is routed to one, and
where a new family or engine goes. Which formats HarborRAG reads and how to configure
the parsers is described for users in
[Parser configuration](../../../../../docs/users/configuration/parser-config.md).

## Families and engines

Parsers are grouped by document family. The root registry decides which family owns an
input; the family owns its engines, its routing between them, quality checks, fallback
and normalization.

```text
parsers/
├── registry.py              # MIME type and extension -> parser family
├── factory.py               # builds the registry and its families
├── compat.py                # older class names, kept for migrations
├── common/                  # contracts and mechanics shared by families
├── pdf/                     # PDF workflow and its engines
├── document/                # DOCX, DOC, ODT and EPUB
├── spreadsheet/             # Excel and delimited data
├── presentation/            # PowerPoint, including legacy PPT
├── markup/                  # HTML and Markdown
├── structured/              # JSON, JSONL and NDJSON
├── text/                    # plain text, source code and config files
└── image/                   # image OCR
```

For PDFs the layers look like this:

```text
HarborParserRegistry
    -> HarborPDFParser
        -> PDFEngineRouter
            -> LiteParsePDFEngine
            -> PyMuPDFEngine
            -> DoclingPDFEngine
            -> MinerUPDFEngine
            -> PaddleOCRPDFEngine
```

Engines are never MIME or extension routes in the root registry. They stay inside their
family and never import one another. The PDF family is described in
[PDF parser family](pdf/README.md).

## Parse a file

`HarborParser` in `common/base.py` is the contract every family implements. It is
deliberately not exported from `harborrag_adapters.parsers`, which exports only
`HarborParserFactory`, `HarborParserRegistry`, `ParseRequest` and `ParseResult`. A
caller submits a `ParseRequest` and gets the same `ParseResult` shape from every family:

```python
import asyncio

from harborrag_adapters.parsers import HarborParserFactory, ParseRequest


async def main() -> None:
    registry = HarborParserFactory().create_registry()
    parser = registry.resolve(filename="technical-report.pdf", mime_type="application/pdf")
    result = await parser.parse(
        ParseRequest(
            source_uri="technical-report.pdf",
            filename="technical-report.pdf",
            mime_type="application/pdf",
        )
    )
    print(result.parser_name, result.engine_name, [attempt.engine for attempt in result.attempts])


asyncio.run(main())
```

Run in the repository's workspace environment from a folder that holds a one-page
`technical-report.pdf`, it printed `pdf liteparse ['liteparse']`: the `pdf` family
accepted LiteParse's output on the first attempt.

`ParseResult` holds the normalized `elements` and `text`, the `parser_name` (family)
and `engine_name`, `warnings`, `metadata`, and `attempts`, the ordered history of
engines tried. `registry.parse_request(request)` resolves and parses in one call.

Ingestion uses the synchronous `registry.parse(...)` instead. It accepts a `RawDocument`,
a `ParseInput`, bytes or a `Path`, and returns a core `ParsedDocument`; the runtime's
document parser and the connectors' attachment processing call it. Older concrete class
names, such as `PdfParser` or `DocxParser`, are importable from `compat.py` only; they are
not part of the public API.

## Routing

The registry indexes only complete family parsers.

- When the MIME type and the extension point at the same family, that family is used.
- A known extension wins over a generic transport type such as
  `application/octet-stream`.
- An unknown extension with a generic MIME type is unsupported: a `.rtf` file reported
  as `text/plain` is not parsed as plain text.
- When the MIME type and the extension point at two different specific families, the
  parse fails instead of guessing.
- `register_extension()` and `register_mime_type()` accept builders for an application
  that needs its own construction.
- `HarborParserFactory` registers the default families from `ParserConfig`.

Inside a family, a second routing layer picks the engine. Most families pick one engine
by format. The PDF family follows a named profile from its configuration and can try
several engines until its quality policy accepts a result.

## Optional dependencies

The text, Markdown and JSON parsers need no extra. The other families need the
`parsers` extra, one of the narrower family extras, or a PDF extra; the list is in
[Installation: add parser and PDF extras](../../../../../docs/getting-started/installation.md#add-parser-and-pdf-extras).
Engine imports stay lazy, so a family can be built while an engine it does not use is
missing its dependency.

## Add a family or an engine

The checklist for a new parser is in
[Extending HarborRAG: parsers](../../../../../docs/developers/extending/README.md#parsers).
Inside this folder:

- A new family implements `HarborParser` in `<family>/parser.py`, declares its MIME
  types and extensions, and defines its engine contract in `<family>/base.py`. The
  factory then registers it.
- A new engine goes under `<family>/engines/<engine>/`: `config.py` holds its settings,
  `engine.py` integrates only that engine, and `mapping.py` converts its output when
  needed. The family's router receives the engine through dependency injection.
- Move behaviour into `common/` only once at least two families use it. PDF page
  analysis, OCR policy, spreadsheet cells, presentation notes and DOM cleanup stay in
  their family.

## Logging and tests

Parsers log under `harborrag.adapters.parsers`. A family logs engine selection and
failures, never the extracted document text.

Tests live in `packages/harborrag-adapters/tests/parsers/`, in `unit/` (which includes
the family architecture tests), `failure/`, `security/`, `performance/` and `smoke/`.
