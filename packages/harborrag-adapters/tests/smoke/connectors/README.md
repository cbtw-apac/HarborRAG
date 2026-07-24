# Connector smoke checks

These scripts perform real connector discovery and load operations through
`HarborConnector`, using the same declarative sources as the application:
`config/connectors.yaml` for connector settings and `config/parsers.yaml` for
attachment/document parsing. They verify authentication, source scoping,
API/filesystem access, mapping into `SourceRecord`, and real content parsing.
Confluence and JIRA also repeat the load with attachment processing enabled;
Local parses the discovered file directly (PDF, DOCX, images, and everything
else `HarborParser` supports).

They are manual checks, not pytest tests. Read the shared [smoke-test safety and
exit-code guidance](../README.md) before using real credentials or content.

## Prerequisites

- Run commands from the repository root with Python 3.12.
- Use a source containing at least one readable, non-sensitive document.
- Ensure the machine can reach the selected provider.
- Grant credentials access only to the test repository, space, project, site,
  or drive being exercised.

The base connector clients are installed with the adapter:

```bash
uv sync --package harborrag-adapters
```

Real parsing needs the parser dependencies, plus PDF/OCR engines. These
scripts parse PDFs with Docling and images with RapidOCR by default:

```bash
uv sync --package harborrag-adapters --extra parsers --extra pdf
```

The `pdf` extra explicitly installs both `rapidocr` and its default CPU
inference runtime, `onnxruntime`. Docling acceleration (CPU/CUDA/MPS/XPU) is
configured through `config/parsers.yaml`.

## Configuration

Connector settings live in `config/connectors.yaml`; credentials live in
`env/.env.connector`. Both fall back to their `.example` counterparts
(`config/connectors.example.yaml`, `env-example/.env.connector.example`) when
the real file doesn't exist yet, so copy the example and fill in your values:

```bash
cp config/connectors.example.yaml config/connectors.yaml
cp env-example/.env.connector.example env/.env.connector
```

| Connector | Required environment variables | Optional |
| --- | --- | --- |
| Local | `LOCAL_SOURCE_PATH` | None |
| Confluence | `CONFLUENCE_BASE_URL`, `CONFLUENCE_SPACE_KEY`, `CONFLUENCE_TOKEN` | `CONFLUENCE_EMAIL` for Cloud |
| JIRA | `JIRA_BASE_URL` and either `JIRA_TOKEN` or `JIRA_API_TOKEN` | `JIRA_EMAIL` for Cloud |

Everything else — content type filters, attachment limits, pagination, JIRA
`project_keys` scoping, and so on — is a literal setting in
`config/connectors.yaml`. Edit that file directly instead of adding more
environment variables. Relative `LOCAL_SOURCE_PATH` values are resolved from
the repository root.

GitHub and SharePoint smoke checks (`github.py`, `sharepoint.py`) are
unaffected by this catalog and keep reading directly from environment
variables; see their required variables in the module docstring of each file.

## Run a connector

```bash
python packages/harborrag-adapters/tests/smoke/connectors/run.py --connector local
python packages/harborrag-adapters/tests/smoke/connectors/run.py --connector confluence
python packages/harborrag-adapters/tests/smoke/connectors/run.py --connector jira
```

`confluence.py`, `jira.py`, and `local.py` remain as thin, no-argument
entry points (`python .../jira.py`) for direct use and for `run_all.py`.

After individual checks pass, run every configured provider (including GitHub
and SharePoint):

```bash
python packages/harborrag-adapters/tests/smoke/connectors/run_all.py
```

`run_all.py` skips providers returning `2`, fails when any configured provider
returns `1`, and returns `2` when none are configured.

## Save parsed output

By default nothing is written to disk. Pass `--output txt` or `--output md` to
save the parsed content under `tests/smoke/connectors/output/` (override with
`--output-dir`):

```bash
python packages/harborrag-adapters/tests/smoke/connectors/run.py --connector jira --output txt
python packages/harborrag-adapters/tests/smoke/connectors/run.py --connector jira --output md
```

`txt` saves a flat concatenation of the body and every parsed attachment's
text. `md` saves a structured Markdown document instead: a `#` title, a
metadata list (source, content type), the body, and each parsed attachment
under its own `###` heading.

`md` output also makes images actually viewable: image attachments
(Confluence/JIRA) are downloaded into a `<output-file-stem>.assets/` sibling
directory and embedded with `![title](stem.assets/filename)`; a local image
file is embedded with a `file://` link to its original path. `txt` output has
no such folder — it's OCR text only, since plain text can't reference a file.

For Confluence/JIRA this covers the page/issue body plus every parsed
attachment's text. For Local it saves the real parsed content of the
discovered file (not raw bytes). Use `--limit` to change how many records are
discovered and processed — each gets its own output file (default: 3).

## What each check verifies

| Target | Discovery limit | Required result |
| --- | ---: | --- |
| Local | 5 (3 via `run.py` default) | At least one record and a successful real parse of the first file |
| GitHub | 3 | At least one repository file and a successful blob load |
| Confluence | 3 | First page loads both without and with attachment processing |
| JIRA | 3 | First issue loads both without and with attachment processing |
| SharePoint | 3 | At least one drive item and a successful first-file download |

Confluence and JIRA fail if an attempted attachment ends in `failed` or
`unsupported`. A source with no attachments can still pass.

## Parser selection

PDF and image parsing come from `config/parsers.yaml` (falling back to
`config/parsers.example.yaml`), the same catalog the application uses. The
shipped default enables `pdf-docling`, which parses PDFs with Docling and OCRs
scanned pages with RapidOCR. Plain image attachments and local image files
always OCR through RapidOCR — that routing isn't expressible in the
declarative parser catalog, so the smoke bootstrap wires it directly.

To use a different PDF backend or profile, edit `config/parsers.yaml` (see
`config/parsers.example.yaml` for the other available blocks: `pdf-default`,
`pdf-balanced`, `pdf-ocr`, `pdf-quality`, `pdf-pymupdf-only`, `pdf-liteparse-*`,
`pdf-mineru-*`, `pdf-paddleocr`) — enable exactly one PDF parser definition.

## Output and troubleshooting

Successful output includes discovered IDs, media types, character counts, and
attachment status/count information. Full provider content is not printed
unless `HARBOR_SMOKE_VERBOSE=1` is set (bounded, redacted previews; disabled
in CI).

- Exit `2`: check `config/connectors.yaml` and `env/.env.connector` — the
  printed message names the missing/undefined connector or variable.
- No records: confirm the configured source contains readable documents and
  that repository/space/project/drive scoping is correct.
- Authentication failures: verify Cloud email requirements, token scopes,
  tenant/client credentials, VPN, proxy, and provider URL.
- Attachment/parse failures: install parser extras (`--extra parsers --extra
  pdf`), verify the attachment type and size, and check `config/parsers.yaml`.
