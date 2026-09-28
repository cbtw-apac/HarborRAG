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
exit-code guidance](../../README.md#real-system-smoke-tests) before using real
credentials or content.

## Prerequisites

- Run commands from the repository root with Python 3.12.
- Use a source containing at least one readable, non-sensitive document.
- Ensure the machine can reach the selected provider.
- Grant credentials access only to the test repository, space, project, site,
  or drive being exercised.

One workspace sync installs the connector clients, the parser dependencies,
and the OCR engines the parser catalog enables. The shipped
`config/parsers.yaml` parses PDFs and raster images through the same LiteParse
OCR server, and the root `dev` dependency group pulls
`harborrag-adapters[parsers,pdf-liteparse]`, so `uv run` already has them:

```bash
uv sync
```

Do not narrow this to `uv sync --package harborrag-adapters`: that drops
`harborrag-runtime`'s own dependencies, which these scripts import from
source, and the run then fails on `No module named 'yaml'`.

Add `--extra image-rapidocr` (local ONNX image OCR) or `--extra pdf` (every
PDF engine: Docling, MinerU, PaddleOCR, PyMuPDF) only when exercising one of
the commented-out alternatives in `config/parsers.yaml`.

LiteParse OCRs scanned pages through the OCR server named by
`pdf-liteparse.engines[].settings.ocr_server_url`, so that server must be
running before a PDF with scanned pages can be parsed. See
[Parser selection](#parser-selection) for how these scripts reach it from the
host.

## Configuration

Connector settings live in `config/connectors.yaml`; credentials live in
`env/.env.connector`. Both fall back to their `.example` counterparts
(`config/connectors.example.yaml`, `env-example/.env.connector.example`) when
the real file doesn't exist yet, so copy the example and fill in your values:

```bash
cp config/connectors.example.yaml config/connectors.yaml
cp env-example/.env.connector.example env/.env.connector
```

Each key under `connectors` is an application-level `connection_id`, not a
provider name. For example, `harborrag-workspace` may use the `local` provider
and `jira-main` may use the `jira` provider. Smoke commands select these IDs so
multiple connections can be configured for the same provider.

| Connector | Required environment variables | Optional |
| --- | --- | --- |
| Local | `LOCAL_SOURCE_PATH` | None |
| GitHub | Environment variables referenced by the selected connection, normally `GITHUB_REPOSITORY_URL`, `GITHUB_TOKEN` | Provider settings such as branch/root path stay in YAML |
| Confluence | `CONFLUENCE_BASE_URL`, `CONFLUENCE_SPACE_KEY`, `CONFLUENCE_TOKEN` | `CONFLUENCE_EMAIL` for Cloud |
| JIRA | `JIRA_BASE_URL` and either `JIRA_TOKEN` or `JIRA_API_TOKEN` | `JIRA_EMAIL` for Cloud |
| SharePoint | Environment variables referenced by the selected connection, normally Microsoft tenant/client credentials and `SHAREPOINT_SITE_URL` | Drive/root path stay in YAML |

Everything else - content type filters, attachment limits, pagination, JIRA
`project_keys` scoping, and so on - is a literal setting in
`config/connectors.yaml`. Edit that file directly instead of adding more
environment variables. Relative `LOCAL_SOURCE_PATH` values are resolved from
the repository root.

GitHub and SharePoint now use the same catalog resolution and environment
references as every other smoke check.

## Run a connector

```bash
python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector harborrag-workspace
python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector confluence-main
python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector jira-main
```

For convenience, a provider name such as `--connector jira` is accepted when
exactly one enabled connection uses it. Pass the connection ID when multiple
connections use the same provider. The provider modules remain thin,
no-argument entry points; they also require a unique enabled connection for
their provider.

After individual checks pass, run every enabled configured connection:

```bash
python packages/harborrag-adapters/tests/connectors/smoke/run_all.py
```

`run_all.py` dispatches by each definition's `provider`, fails when any enabled
connection fails, and returns `2` when no enabled connection has a smoke
runner.

## Save parsed output

By default nothing is written to disk. Pass `--output txt` or `--output md` to
save the parsed content under `tests/connectors/smoke/output/` (override with
`--output-dir`):

```bash
python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector jira-main --output txt
python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector jira-main --output md
```

Saved output is currently supported by Local, Confluence, and JIRA runners;
GitHub and SharePoint reject `--output` explicitly.

`txt` saves a flat concatenation of the body and every parsed attachment's
text. `md` saves a structured Markdown document instead: a `#` title, a
short header list (source, content type), a `## Metadata` section with a
curated set of provider-specific fields (Jira: issue key, status, assignee,
priority, labels, ...; Confluence: space, version, author, labels, breadcrumb,
...; Local: parser, PDF engine, page count, OCR settings, figures extracted,
warnings - whichever fields have a value), and the body. Confluence also adds
an `## Attachments` section with each parsed attachment under its own `###`
heading; the JIRA renderer deliberately saves the issue body only, so use
`--output txt` to inspect JIRA attachment text.

`md` output also makes images actually viewable:

- Image attachments (Confluence/JIRA) are downloaded into a
  `<output-file-stem>.assets/` sibling directory and embedded with
  `![title](stem.assets/filename)`.
- A local image *file* (the discovered record itself, e.g. a `.png`) is
  embedded with a `file://` link to its original path.
- Figures embedded *inside* a locally parsed PDF are copied into the same
  `<output-file-stem>.assets/` convention and listed under a `## Figures`
  heading. This is Docling-only: it requires `pdf-docling.image_output_dir` to
  be set in `config/parsers.yaml` (see
  [Parser selection](#parser-selection)), and full-page renders and table
  crops Docling can also produce are intentionally skipped to keep output
  focused on actual figures. The shipped LiteParse parser reports no image
  paths, so its output simply has no `## Figures` section.

`txt` output has no such folder - it's OCR text only, since plain text can't
reference a file.

For Confluence/JIRA this covers the page/issue body plus every parsed
attachment's text. For Local it saves the real parsed content of the
discovered file (not raw bytes). Use `--limit` to change how many records are
discovered and processed - each gets its own output file (default: 3).

## What each check verifies

| Target | Discovery limit | Required result |
| --- | ---: | --- |
| Local | 5 (3 via `run.py` default) | At least one record and a successful real parse of the first file |
| GitHub | 3 | At least one repository file and a successful blob load |
| Confluence | 3 | First page loads without attachments; also loads with attachments if `include_attachments: true` in config |
| JIRA | 3 | First issue loads without attachments; also loads with attachments if `include_attachments: true` in config |
| SharePoint | 3 | At least one drive item and a successful first-file download |

The attachment pass only runs when the connector's `include_attachments`
setting in `config/connectors.yaml` is `true`; when it's `false`, the check
prints a skip message and passes without touching attachments. When the pass
does run, Confluence and JIRA fail if an attempted attachment ends in
`failed` or `unsupported`. A source with no attachments can still pass.

Local fails only on a genuine parse error, or on a parser returning empty
content for a file that itself has non-blank bytes. A source file that is
itself empty or whitespace-only still passes, matching Confluence/JIRA's
tolerance of blank page/issue bodies.

## Parser selection

PDF and image parsing come from `config/parsers.yaml` (falling back to
`config/parsers.example.yaml`), the same catalog the application uses. The
shipped default enables `pdf-liteparse`, which parses PDFs with LiteParse and
OCRs scanned pages through an external OCR server. There is no environment
override for the engine: change the enabled definition in
`config/parsers.yaml` and the smoke scripts follow it. Every run prints the
engines it resolved, for example:

```text
[parsers] pdf engines=liteparse
[parsers] image engines=rapidocr
```

Plain image attachments and local image files always OCR through RapidOCR -
that routing isn't expressible in the declarative parser catalog, so the smoke
bootstrap wires it directly. On first use the smoke helper reports the ONNX
Runtime providers it can see and reuses one loaded RapidOCR engine for all
attachments.

### LiteParse OCR server

`config/parsers.yaml` is written for the containerized ingestion worker, so
`ocr_server_url` names the `ppocr-server` Docker DNS alias on
`harborrag-data-network`. These scripts run on the host, where that name does
not resolve and LiteParse would fail every scanned page with
`OCR failed: ... error sending request`. The bootstrap therefore keeps the
scheme, port, and path but retargets an unresolvable host at the loopback
interface, and says so:

```text
[parsers] liteparse ocr_server_url='http://ppocr-server:8888/ocr' is not
reachable from this host; using 'http://localhost:8888/ocr'
```

That assumes the OCR server publishes its port on the host, which the
`ppocr-server` container normally does (`-p 8888:8888`), and that the port in
`ocr_server_url` is the published one. `HARBORRAG_OCR_SERVER_URL` (read by
`config/parsers.yaml` itself) changes what the catalog configures for every
process, including the worker. Point only the smoke run at a different OCR
server with:

```bash
HARBOR_SMOKE_OCR_SERVER_URL=http://ocr.internal:8888/ocr \
  python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector jira-main
```

A resolvable configured host - including a run inside the container - is used
exactly as configured.

## Output and troubleshooting

Successful output includes discovered IDs, media types, character counts, and
attachment status/count information. Full provider content is not printed
unless `HARBOR_SMOKE_VERBOSE=1` is set (bounded, redacted previews; disabled
in CI).

- Exit `2`: check `config/connectors.yaml` and `env/.env.connector` - the
  printed message names the missing/undefined connector or variable.
- No records: confirm the configured source contains readable documents and
  that repository/space/project/drive scoping is correct.
- Authentication failures: verify Cloud email requirements, token scopes,
  tenant/client credentials, VPN, proxy, and provider URL.
- Attachment/parse failures: install parser extras (`--extra parsers --extra
  pdf-liteparse`), verify the attachment type and size,
  and check `config/parsers.yaml`.
- `OCR failed: ... error sending request`: the OCR server LiteParse is
  configured to call is unreachable. Start it, or set
  `HARBOR_SMOKE_OCR_SERVER_URL` (see
  [LiteParse OCR server](#liteparse-ocr-server)).
