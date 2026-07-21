# Real smoke-test runbook

These checks answer one question: can the installed adapter complete a small,
real operation in the target environment? They deliberately do not use pytest,
mocks, monkeypatching, recorded HTTP responses, or fake provider clients.

Smoke checks are manual and opt-in. They may use paid APIs, access private
content, download provider models, or require network routes unavailable in a
developer environment. They are not collected by the deterministic test suite.

## Current environment status

The present workspace is not assumed to be the main deployment environment.
An exit code of `2` is therefore expected for a target whose credentials, real
input file, optional parser engine, or network access is not available yet.
Do not weaken a check or replace the real dependency with a mock just to make it
run here.

| Area | Runnable when | What is verified |
| --- | --- | --- |
| Local connector | `LOCAL_SOURCE_PATH` names a real file/directory | Real discovery and first-document load |
| Confluence, GitHub, JIRA, SharePoint | Credentials, network route, and a readable source exist | Real discovery and load; Atlassian also checks attachment mode |
| Chat, embeddings, reranking | A real deployment and credentials/ambient identity exist | One real response, normalized metadata, and family-specific output invariants |
| Parsers | A representative real document and required optional libraries/models exist | Real extraction through registry routing or a selected PDF profile |
| Repository adapters | Local Compose stack or SQLite is available | Real health and write/read/cleanup operations for PostgreSQL, Redis, Qdrant, FalkorDB, and SQLite |

## Safety rules

1. Use a least-privilege test tenant, repository, space, project, drive, and
   provider key. Never point a smoke test at broader production data than
   necessary.
2. Keep secrets in repo-root `.env`, an exported environment, or a protected
   file selected with `HARBOR_SMOKE_ENV_FILE`. Never commit the populated file.
3. Review provider pricing and quotas before model or OCR runs. Smoke model
   configs disable retries/failovers so one target cannot fan out unexpectedly.
4. Default output contains identifiers/counts and never full connector
   document text or model prompts/responses. Connector content previews require
   `HARBOR_SMOKE_VERBOSE=1`, are disabled in CI, and still must not be persisted.
5. Run one target first. Use a group runner only after the individual target
   succeeds.

## Environment files

Use `.env.connector.example` for connector variables and `.env.models.example`
for model variables. Merge only the groups you intend to exercise into the
same repo-root `.env`, or point all scripts to a protected file:

```bash
HARBOR_SMOKE_ENV_FILE=/secure/path/harbor-smoke.env \
  python packages/harborrag-adapters/tests/smoke/models/chat.py
```

Exported process variables take precedence over dotenv values.

## Connector smoke checks

Run one real connector or all configured connectors:

```bash
python packages/harborrag-adapters/tests/smoke/connectors/local.py
python packages/harborrag-adapters/tests/smoke/connectors/jira.py
python packages/harborrag-adapters/tests/smoke/connectors/run_all.py
```

| Connector | Required variables | Optional variables |
| --- | --- | --- |
| Local | `LOCAL_SOURCE_PATH` | None |
| Confluence | `CONFLUENCE_BASE_URL`, `CONFLUENCE_SPACE_KEY`, `CONFLUENCE_TOKEN` | `CONFLUENCE_EMAIL` for Cloud |
| JIRA | `JIRA_BASE_URL` and `JIRA_TOKEN` or `JIRA_API_TOKEN` | `JIRA_PROJECT_KEY`, `JIRA_EMAIL` for Cloud |
| GitHub | `GITHUB_TOKEN` plus `GITHUB_REPOSITORY_URL`, or `GITHUB_OWNER` + `GITHUB_REPO` | `GITHUB_REF` |
| SharePoint | `SHAREPOINT_SITE_URL` or `SHAREPOINT_SITE_ID`, plus `MICROSOFT_GRAPH_TOKEN` or the tenant/client/secret trio | `SHAREPOINT_DRIVE_NAME` |

Success requires at least one discovered record and a successfully normalized
first document. Confluence and JIRA repeat the load with attachments enabled.
Set `HARBOR_SMOKE_PDF_BACKEND=docling` to require Docling for PDF attachments;
this also reuses Docling's RapidOCR runtime for image attachments. Set
`HARBOR_SMOKE_IMAGE_BACKEND=rapidocr` to select it independently. Other
attachment types continue to use the normal Harbor parser registry.

## Model smoke checks

Run a family separately, then optionally run every configured family:

```bash
python packages/harborrag-adapters/tests/smoke/models/chat.py
python packages/harborrag-adapters/tests/smoke/models/embed.py
python packages/harborrag-adapters/tests/smoke/models/rerank.py
python packages/harborrag-adapters/tests/smoke/models/run_all.py
```

Each family requires `HARBOR_SMOKE_<FAMILY>_PROVIDER` and
`HARBOR_SMOKE_<FAMILY>_MODEL`, plus either an API key, explicit cloud
credentials, or `ALLOW_AMBIENT_CREDENTIALS=true` for a provider that supports
ambient identity. See `.env.models.example` for all transport fields.

Chat supports `direct_sdk`, `litellm_router`, and `litellm_proxy` through
`HARBOR_SMOKE_CHAT_BACKEND`. Proxy mode additionally requires
`HARBOR_SMOKE_CHAT_PROXY_API_BASE` and
`HARBOR_SMOKE_CHAT_PROXY_API_KEY`.

Success criteria are intentionally provider-neutral:

- Chat: non-empty normalized text plus provider model and request metadata.
- Embed: two finite vectors with consistent positive dimensions.
- Rerank: two unique, in-range results with finite relevance scores.

## Parser smoke checks

Use representative, non-sensitive source files rather than generated fixtures:

```bash
python packages/harborrag-adapters/tests/smoke/parsers/parse_file.py samples/report.docx
python packages/harborrag-adapters/tests/smoke/parsers/parse_file.py samples/report.pdf --pdf-profile fast
python packages/harborrag-adapters/tests/smoke/parsers/parse_file.py samples/report.pdf --pdf-backend docling
python packages/harborrag-adapters/tests/smoke/parsers/parse_file.py samples/scan.pdf --pdf-profile ocr
```

Maintain a main-environment corpus containing at least one real DOCX, PPTX,
XLSX, CSV, HTML, EPUB, image, Markdown/text, JSON, text PDF, and scanned PDF.
The script succeeds only when routing completes and extracted content is
non-empty. Record the selected parser, content/element counts, warnings, input
kind, and installed optional-engine versions; do not record extracted content.

The `fast` PDF profile can usually run with PyMuPDF. `balanced`, `ocr`, and
`quality` may remain unavailable until Docling, LiteParse, MinerU, PaddleOCR,
their model assets, and suitable CPU/GPU resources are installed in the main
environment.

## Repository smoke checks

Start the local services, install their optional clients, and run the group
check:

```bash
DATABASE_ENV_FILE=env/.env.database ./scripts/deployment/database_up.sh
uv pip install -e \
  "packages/harborrag-adapters[redis,qdrant,falkordb,postgres]"
HARBOR_SMOKE_ENV_FILE=env/.env.database \
  .venv/bin/python packages/harborrag-adapters/tests/smoke/repositories/run_all.py
```

SQLite uses a temporary database and needs no running service. The other checks
default to the ports in `docker-compose.database.yml`. See
[`repositories/README.md`](repositories/README.md) for individual commands,
overrides, operations, and cleanup behavior.

## Exit codes and evidence

| Code | Meaning | Action |
| --- | --- | --- |
| `0` | Real operation passed | Record target, sanitized configuration, dependency versions, and timestamp |
| `1` | Target was configured but the real operation failed | Investigate configuration, service response, normalization, or output invariant |
| `2` | Target is unavailable/not configured in this environment | Track as pending; do not convert it to a fake test |

For release evidence, capture only the command, exit code, UTC timestamp,
adapter commit, provider/parser identifier, and safe counts/latency. Never
attach dotenv files, headers, raw provider payloads, prompts, responses, or
document text.
