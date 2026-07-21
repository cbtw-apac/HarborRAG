# Connector smoke checks

These scripts perform real connector discovery and load operations through
`HarborConnector`. They verify authentication, source scoping, API/filesystem
access, mapping into `SourceRecord`, and loading the first `RawDocument`.
Confluence and JIRA also repeat the load with attachment processing enabled.

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

Attachment parsing needs the normal parser dependencies. Selected PDF/OCR
engines may also download models or require substantial CPU/GPU resources:

```bash
uv sync --package harborrag-adapters --extra parsers --extra pdf
```

The `pdf` extra explicitly installs both `rapidocr` and its default CPU
inference runtime, `onnxruntime`. RapidOCR's own documentation recommends this
CPU runtime; Docling acceleration is configured separately through PyTorch.

## Configuration

Copy only the provider blocks you intend to run from
`env-example/.env.connector.example` into an untracked repo-root `.env`, or
select another protected file with `HARBOR_SMOKE_ENV_FILE`.

| Connector | Required variables | Optional variables |
| --- | --- | --- |
| Local | `LOCAL_SOURCE_PATH` | None |
| GitHub | `GITHUB_TOKEN` and either `GITHUB_REPOSITORY_URL` or `GITHUB_OWNER` + `GITHUB_REPO` | `GITHUB_REF` |
| Confluence | `CONFLUENCE_BASE_URL`, `CONFLUENCE_SPACE_KEY`, `CONFLUENCE_TOKEN` | `CONFLUENCE_EMAIL` for Cloud |
| JIRA | `JIRA_BASE_URL` and either `JIRA_TOKEN` or `JIRA_API_TOKEN` | `JIRA_PROJECT_KEY`, `JIRA_EMAIL` for Cloud |
| SharePoint | Either `SHAREPOINT_SITE_URL` or `SHAREPOINT_SITE_ID`, plus an authentication option below | `SHAREPOINT_DRIVE_NAME` |

SharePoint authentication can use `MICROSOFT_GRAPH_TOKEN` or all three client
credential fields: `MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID`, and
`MICROSOFT_CLIENT_SECRET`.

Relative `LOCAL_SOURCE_PATH` values are resolved from the repository root.

## Run a connector

Run one configured provider first:

```bash
python packages/harborrag-adapters/tests/smoke/connectors/local.py
python packages/harborrag-adapters/tests/smoke/connectors/github.py
python packages/harborrag-adapters/tests/smoke/connectors/confluence.py
python packages/harborrag-adapters/tests/smoke/connectors/jira.py
python packages/harborrag-adapters/tests/smoke/connectors/sharepoint.py
```

After individual checks pass, run every configured provider:

```bash
python packages/harborrag-adapters/tests/smoke/connectors/run_all.py
```

`run_all.py` skips providers returning `2`, fails when any configured provider
returns `1`, and returns `2` when none are configured.

## What each check verifies

| Target | Discovery limit | Required result |
| --- | ---: | --- |
| Local | 5 | At least one record and a successful first-file byte load |
| GitHub | 3 | At least one repository file and a successful blob load |
| Confluence | 3 | First page loads both without and with attachment processing |
| JIRA | 3 | First issue loads both without and with attachment processing |
| SharePoint | 3 | At least one drive item and a successful first-file download |

Confluence and JIRA fail if an attempted attachment ends in `failed` or
`unsupported`. A source with no attachments can still pass.

## Attachment parser selection

By default, attachment content routes through `HarborParser`. To require an
exact PDF backend, set one of `docling`, `liteparse`, `mineru`, `paddleocr`, or
`pymupdf`:

```bash
HARBOR_SMOKE_PDF_BACKEND=docling \
  python packages/harborrag-adapters/tests/smoke/connectors/confluence.py
```

Docling defaults to `auto`, asks Docling's accelerator resolver for the best
available device, and prints both the requested and resolved values before the
smoke check. Override it with `auto`, `cpu`, `cuda`, `cuda:N`, `mps`, or `xpu`:

```bash
HARBOR_SMOKE_PDF_BACKEND=docling \
HARBOR_SMOKE_DOCLING_DEVICE=xpu \
  python packages/harborrag-adapters/tests/smoke/connectors/confluence.py
```

CUDA and XPU require a matching accelerator-enabled PyTorch build; MPS requires
supported Apple hardware. Keep `auto` for portable configuration and CPU
fallback.

Set `HARBOR_SMOKE_IMAGE_BACKEND=rapidocr` to OCR image attachments. Selecting
Docling as the PDF backend also selects RapidOCR for images unless explicitly
overridden. On first use the smoke helper reports the ONNX Runtime providers it
can see and reuses one loaded RapidOCR engine for all attachments.

## Output and troubleshooting

Successful output includes discovered IDs, media types, character counts, and
attachment status/count information. Full provider content is not printed.

- Exit `2`: check the variable names, selected dotenv path, and local path
  existence.
- No records: confirm the configured source contains readable documents and
  that repository/space/project/drive scoping is correct.
- Authentication failures: verify Cloud email requirements, token scopes,
  tenant/client credentials, VPN, proxy, and provider URL.
- Attachment failures: install parser extras, verify the attachment type and
  size, and test the selected PDF/OCR engine independently.
- To inspect bounded redacted previews locally, set `HARBOR_SMOKE_VERBOSE=1`.
