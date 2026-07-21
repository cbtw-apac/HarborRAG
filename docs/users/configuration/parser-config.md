# Parser Configuration

The runtime loader reads named parser definitions from versioned YAML. Start from [`config/parsers.example.yaml`](../../../config/parsers.example.yaml), which enables a credential-free `fast` PDF profile.

## Default parser registry

`HarborParser` routes by filename suffix and MIME type. Its default stack supports PPTX/PPTM, DOCX, Excel, CSV/TSV, images, HTML/XHTML, EPUB, JSON/JSONL/NDJSON, Markdown/MDX, PDFs, and plain text/source/config formats.

An enabled catalog definition replaces the matching parser type in that default stack. Parser types not configured in YAML remain available.

## PDF profile

```yaml
version: 1

parsers:
  pdf-default:
    parser: pdf
    enabled: true
    settings:
      profile: fast
      min_content_chars: 20
```

Built-in PDF profiles are `fast`, `balanced`, `ocr`, and `quality`. They select an ordered backend chain maintained by `PdfParser`. Start with `fast` for PDFs containing embedded text; heavier profiles may need model downloads, OCR runtimes, more memory, and substantially more compute.

## Explicit backend chain

```yaml
parsers:
  pdf-layout:
    parser: pdf
    enabled: true
    settings:
      min_content_chars: 20
    engines:
      - backend: pymupdf
      - backend: docling
        settings:
          do_ocr: true
          do_table_structure: true
```

Supported backend names are `pymupdf`, `docling`, `liteparse`, `mineru`, and `paddleocr`. An explicit `engines` chain cannot also set `profile`. Backend settings are strict and checked against typed option dataclasses.

## Environment and secrets

Backend secrets use `<field>_env`:

```yaml
- backend: liteparse
  secrets:
    password_env: PDF_DOCUMENT_PASSWORD
```

MinerU subprocess environment forwarding maps a target variable to a source variable:

```yaml
- backend: mineru
  settings:
    backend: vlm-http-client
    server_url: http://127.0.0.1:30000
  environment:
    MINERU_VL_API_KEY: OPENAI_API_KEY
```

The referenced process variable must exist and be non-empty when the parser is
built. `env-example/.env.parser.example` lists optional values, but HarborRAG
does not load it automatically.

## Load and build

```python
from harborrag_runtime.config import load_parser_catalog

catalog = load_parser_catalog("config/parsers.example.yaml")
print(catalog.names(enabled_only=True))

pdf = catalog.build("pdf-default")
parser_registry = catalog.build_harbor_parser()
```

Only one enabled definition may replace a given stable parser name. `build_harbor_parser()` rejects conflicting enabled definitions instead of choosing silently.

Code overrides take precedence over YAML settings:

```python
pdf = catalog.build("pdf-default", overrides={"min_content_chars": 100})
```

Construction validates configuration. A backend whose optional library, executable, model, device, or service is unavailable reports that availability failure when parsing.
