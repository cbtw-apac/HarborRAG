# Parser Configuration

The runtime loader reads named parser definitions from versioned YAML. The
checked-in [`config/parsers.yaml`](../../../config/parsers.yaml) activates
Docling with RapidOCR for PDFs and RapidOCR for raster images. Alternative
profiles and engines remain in the file as commented blocks.

## Default parser registry

`HarborParser` routes by filename suffix and MIME type. Its default stack supports PPTX/PPTM, DOCX, Excel, CSV/TSV, images, HTML/XHTML, EPUB, JSON/JSONL/NDJSON, Markdown/MDX, PDFs, and plain text/source/config formats.

An enabled catalog definition replaces the matching parser type in that default stack. Parser types not configured in YAML remain available.

## Active Docling PDF parser

```yaml
version: 1

parsers:
  pdf-docling:
    parser: pdf
    enabled: true
    settings:
      min_content_chars: 20
    engines:
      - backend: docling
        settings:
          do_ocr: true
          ocr_engine: rapidocr
          do_table_structure: true
```

An explicit one-item engine chain guarantees that PDFs go through Docling.
Using a built-in profile would create a fallback chain and could select another
engine first. Available profiles are `fast`, `balanced`, `ocr`, and `quality`.

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

## Image OCR engine

```yaml
parsers:
  image-rapidocr:
    parser: image
    enabled: true
    settings:
      ocr_engine: rapidocr
      max_pixels: 100000000
```

Image OCR supports `rapidocr` and `pytesseract`. RapidOCR is loaded lazily and
one engine instance is reused by the configured parser. The `lang`, `config`,
and `timeout` settings apply to the `pytesseract` alternative.

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

catalog = load_parser_catalog("config/parsers.yaml")
print(catalog.names(enabled_only=True))

pdf = catalog.build("pdf-docling")
image = catalog.build("image-rapidocr")
parser_registry = catalog.build_harbor_parser()
```

Only one enabled definition may replace a given stable parser name. `build_harbor_parser()` rejects conflicting enabled definitions instead of choosing silently.

Code overrides take precedence over YAML settings:

```python
pdf = catalog.build("pdf-docling", overrides={"min_content_chars": 100})
```

Construction validates configuration. A backend whose optional library, executable, model, device, or service is unavailable reports that availability failure when parsing.
