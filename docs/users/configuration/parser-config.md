# Parser Configuration

The runtime loader reads named parser definitions from versioned YAML. The
checked-in [`config/parsers.yaml`](../../../config/parsers.yaml) activates
LiteParse (with an external OCR server) for both PDFs and raster
images. Alternative profiles and engines remain in the file as commented
blocks.

## Default parser registry

`HarborParser` routes by filename suffix and MIME type. Its default stack supports PPTX/PPTM, DOCX, Excel, CSV/TSV, images, HTML/XHTML, EPUB, JSON/JSONL/NDJSON, Markdown/MDX, PDFs, and plain text/source/config formats.

An enabled catalog definition replaces the matching parser type in that default stack. Parser types not configured in YAML remain available.

## Active LiteParse PDF parser

```yaml
version: 1

parsers:
  pdf-liteparse:
    parser: pdf
    enabled: true
    settings:
      min_content_chars: 20
    engines:
      - backend: liteparse
        settings:
          output_format: markdown
          ocr_enabled: true
          ocr_language: en
          ocr_server_url: ${HARBORRAG_OCR_SERVER_URL:-}
          dpi: 150
          num_workers: 3
          strip_code_fences: true
```

An explicit one-item engine chain guarantees that PDFs go through LiteParse.
Using a built-in profile would create a fallback chain and could select another
engine first. Available profiles are `liteparse` (the default when a PDF
definition names neither `engines` nor `profile`: LiteParse, then PyMuPDF),
`fast`, `balanced`, `ocr`, and `quality`. The image parser's default
`ocr_engine` is `liteparse` as well.
`ocr_server_url` points at a self-hosted OCR service; LiteParse calls it for
scanned pages instead of running OCR in-process. It comes from
`HARBORRAG_OCR_SERVER_URL` in `env/.env.parser`. When that is unset or empty no
OCR server is used and LiteParse OCRs locally with Tesseract.

For the containerized ingestion worker use a Docker DNS name rather than
`localhost`, which would be the worker itself: the OCR server joins the
external `harborrag-data-network` under the `ppocr-server` alias, so set
`HARBORRAG_OCR_SERVER_URL=http://ppocr-server:8888/ocr` (the container port,
not the published host port). Use `http://localhost:8888/ocr` when running the
parser directly on the host.

`strip_code_fences` removes Markdown code-fence delimiters from LiteParse's
output. Its layout pass fences any block it reads as preformatted, which on a
CV or a scanned page is ordinary prose, sometimes tagged with a guessed
language such as ```` ```python ````. Only the delimiter lines are dropped, so
no extracted text is lost; set it to `false` to keep LiteParse's fences.

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
  image-liteparse:
    parser: image
    enabled: true
    settings:
      ocr_engine: liteparse
      ocr_server_url: ${HARBORRAG_OCR_SERVER_URL:-}
      lang: en
      max_pixels: 100000000
```

`liteparse` sends raster images to the same OCR server as scanned PDF pages
(or OCRs them locally with Tesseract when no server is set), so the worker
needs no second local inference runtime. Pillow still decodes
the image first, so `max_pixels` and decompression-bomb limits apply before
anything is uploaded. The alternatives are `rapidocr` (local ONNX, needs the
`image-rapidocr` extra) and `pytesseract` (external Tesseract binary); neither
uses `ocr_server_url`.

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

Any setting value may also reference the environment directly with
`${VARIABLE}` or `${VARIABLE:-default}`, which is how the OCR server URL above
moves between a containerized worker and a host run. A bare `${VARIABLE}` that
is unset fails the load; give it a default to make it optional. Credentials do
not belong here: settings named after a secret field are rejected whatever
their value, so they keep using the `secrets` block.

## Load and build

```python
from harborrag_runtime.config import load_parser_catalog

catalog = load_parser_catalog("config/parsers.yaml")
print(catalog.names(enabled_only=True))

pdf = catalog.build("pdf-liteparse")
image = catalog.build("image-liteparse")
parser_registry = catalog.build_harbor_parser()
```

Only one enabled definition may replace a given stable parser name. `build_harbor_parser()` rejects conflicting enabled definitions instead of choosing silently.

Code overrides take precedence over YAML settings:

```python
pdf = catalog.build("pdf-liteparse", overrides={"min_content_chars": 100})
```

Construction validates configuration. A backend whose optional library, executable, model, device, or service is unavailable reports that availability failure when parsing.
