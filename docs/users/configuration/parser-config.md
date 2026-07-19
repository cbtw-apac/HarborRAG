# Parser Configuration

`config/parsers.yaml` defines named parser profiles. The repository enables one
credential-free `fast` profile. Every heavier alternative is a commented YAML
example, so it cannot accidentally load models, call an endpoint, or conflict
with the enabled PDF definition.

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

Supported PDF profiles are `fast`, `balanced`, `ocr`, and `quality`. Profiles
use the backend order maintained by `PdfParser`. Start with `fast` for ordinary
PDFs containing embedded text. Use `balanced` for mixed documents, `ocr` for
mostly scanned documents, and `quality` only when the additional model and
compute cost is justified.

## Explicit PDF engines

Use `engines` when backend order or backend-specific settings must be explicit:

```yaml
parsers:
  pdf-ocr-custom:
    parser: pdf
    enabled: true
    settings:
      min_content_chars: 20
    engines:
      - backend: pymupdf
      - backend: docling
        settings:
          do_ocr: true
          force_full_page_ocr: true
          do_table_structure: true
```

Available backend names are `pymupdf`, `docling`, `liteparse`, `mineru`, and
`paddleocr`. An explicit `engines` chain cannot also specify `profile`.
Backend options are checked against the backend's typed options dataclass.
Python-only objects cannot be stored in YAML.

The repository example contains complete, commented configurations for every
backend. Comment out `pdf-default`, then uncomment exactly one alternative:

| Backend | Advanced controls |
|---|---|
| `pymupdf` | Fast extraction for PDFs that already contain text; it does not run OCR in this backend. |
| `docling` | Layout-aware extraction, tables, and selective or full-page OCR. OCR, table structure, and enrichment features increase processing time. |
| `liteparse` | Local parsing with optional OCR, an HTTP OCR endpoint, page controls, and environment-backed encrypted-PDF passwords. |
| `mineru` | Local pipeline parsing or a lightweight client for an OpenAI-compatible endpoint serving a MinerU document VLM. |
| `paddleocr` | OCR-heavy local document analysis with device, precision, orientation, unwarping, table, formula, chart, and model controls. |

## Backend secrets and environment

Typed backend secrets use the same `<field>_env` form as connector secrets:

```yaml
- backend: liteparse
  settings:
    ocr_server_url: http://127.0.0.1:8080
  secrets:
    password_env: PDF_DOCUMENT_PASSWORD
```

MinerU runs as a subprocess, so selected environment variables can be forwarded
without exposing their values in YAML:

```yaml
- backend: mineru
  settings:
    backend: vlm-http-client
    server_url: http://127.0.0.1:30000
  environment:
    MINERU_VL_API_KEY: OPENAI_API_KEY
    MINERU_VL_MODEL_NAME: OPENAI_VLM_MODEL
```

The left side is the variable MinerU receives; the right side is the source
variable in HarborRAG's process environment. Every referenced variable must be
present and non-empty when the parser is built.

`vlm-http-client` expects an OpenAI-compatible endpoint serving a
MinerU-compatible document VLM. Supplying an arbitrary general-purpose OpenAI
model is not assumed to satisfy MinerU's extraction protocol. A native OpenAI
PDF backend should be implemented as its own parser backend rather than being
represented as MinerU configuration.

LiteParse's `ocr_server_url` points to its HTTP OCR protocol, which can be
implemented by EasyOCR, PaddleOCR, or another compatible OCR service.

## Split environment templates

- `.env.connector.example` contains connector credentials and smoke-test scope.
- `.env.parser.example` contains commented PDF password, OCR, model-cache, and
  remote VLM variables. The active profile needs none of them.
- `.env.example` explains how to combine the templates for a deployment.

HarborRAG reads process environment values. These example files are not loaded
automatically; use your shell, container runtime, secret manager, or deployment
platform to supply them.

## Loading and registry replacement

```python
from harborrag_runtime.config import load_parser_catalog

catalog = load_parser_catalog("config/parsers.yaml")

# Build the enabled named profile.
fast_pdf = catalog.build("pdf-default")

# Replace enabled parser types in HarborParser's default stack.
parser_registry = catalog.build_harbor_parser()
```

`build_harbor_parser()` replaces only parser types with enabled definitions.
For example, an enabled PDF definition replaces the default `PdfParser`, while
Markdown, DOCX, Excel, image, and the other default parsers remain registered.
Only one definition for a given stable parser type may be enabled at once.

Explicit code overrides take precedence over YAML settings:

```python
pdf = catalog.build(
    "pdf-default",
    overrides={"min_content_chars": 100},
)
```

Advanced PDF engines may require optional Python dependencies, model downloads,
GPU runtimes, or system executables. Configuration constructs backend adapters;
the backend still reports a clear availability error when parsing if its
optional dependency is unavailable.
