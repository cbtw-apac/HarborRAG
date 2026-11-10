# Connector smoke checks

These scripts discover and load real records through `HarborConnector`, then parse them,
using the same files as the application: `config/sources/connectors.yaml` for the
connections and `config/ingestion/parsers.yaml` for the parsers. A passing run shows
that sign-in, source scoping, API or filesystem access, mapping into `SourceRecord` and
real parsing all work. Confluence and Jira also reload their records with attachment
processing on.

They are manual scripts, not pytest tests. Read the shared
[safety rules and exit codes](../../README.md#real-system-smoke-tests) before you use
real credentials or content.

## Before you start

- Run every command from the repository root.
- Use the workspace environment from
  [CONTRIBUTING](../../../../../CONTRIBUTING.md#development-setup). The root `dev`
  dependency group adds `harborrag-adapters[parsers,pdf-liteparse]`, the engines the
  shipped `config/ingestion/parsers.yaml` enables, so `uv run` already has them.
- Do not narrow the sync to one package, such as `uv sync --package harborrag-adapters`.
  `uv sync` removes every package the narrowed selection does not need, including the
  `harborrag-runtime` dependencies these scripts import from source.
- To try an engine that `parsers.yaml` leaves commented out, add the adapters extra that
  installs it, for example `pdf` (Docling, MinerU, PaddleOCR, PyMuPDF and RapidOCR):

  ```bash
  uv sync --all-packages --extra dev --extra production --extra pdf
  ```

- Pick a source with at least one readable, non-sensitive document, and give the
  credentials access only to that repository, space, project, site or drive.

## Configure the connections

`config/sources/connectors.yaml` is tracked and ships four enabled connections. Each key
is a connection ID, not a provider name, so one provider can have several connections.
Change their settings as
[Connector configuration](../../../../../docs/users/configuration/connector-config.md)
describes. The scripts read `config/sources/connectors.example.yaml` only when
`connectors.yaml` does not exist.

The scripts load variables from `env/.env.connector` and `env/.env.parser`, which
`scripts/deployment/dev.sh bootstrap` creates from the templates in `env-example/`. Set
`HARBOR_SMOKE_ENV_FILE` to load one other file instead. A variable you already exported
always wins over the file.

| Connection | Provider | Variables it needs |
| --- | --- | --- |
| `harborrag-workspace` | `local` | `LOCAL_SOURCE_PATH`; a relative path resolves against the directory you run the command from |
| `confluence-main` | `confluence` | `CONFLUENCE_BASE_URL`, `CONFLUENCE_SPACE_KEY`, `CONFLUENCE_TOKEN`, and `CONFLUENCE_EMAIL` because the shipped entry is Cloud |
| `jira-main` | `jira` | `JIRA_BASE_URL`, `JIRA_PROJECT_KEY` (fills `project_keys`), `JIRA_TOKEN`, and `JIRA_EMAIL` because the shipped entry is Cloud |
| `sharepoint-main` | `sharepoint` | `SHAREPOINT_SITE_URL`, `MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID`, plus a device-code sign-in |

- Jira: the smoke scripts also accept `JIRA_API_TOKEN` when `JIRA_TOKEN` is empty. The
  application reads only `JIRA_TOKEN`.
- SharePoint: the shipped entry uses `auth_mode: device_code`. Sign in once as
  [Delegated sign-in](../../../../../docs/users/configuration/connector-config.md#delegated-sign-in)
  describes, then export `HARBORRAG_TENANT_ID=DEFAULT`, or the tenant you signed in for.
  Without it the script stops with exit code `2` and
  `auth_mode device_code needs token_cache_path or harborrag_tenant_id`.
- GitHub: no GitHub connection ships. Add one to `connectors.yaml` with the variables it
  references, such as `GITHUB_REPOSITORY_URL` and `GITHUB_TOKEN`, to use the GitHub
  runner.

## Run one connection

```bash
uv run python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector harborrag-workspace
uv run python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector confluence-main
uv run python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector jira-main
```

`--connector` also takes a provider name, such as `--connector jira`, when exactly one
enabled connection uses that provider. `--limit` sets how many records to discover and
process (default `3`).

A passing local run prints the parser engines it resolved and one block per parsed file:

```text
[local] discovered 1 record(s)
  - installation.md (text/markdown)
[parsers] pdf engines=liteparse
[parsers] image engines=liteparse

[local] parsed document
[local] source='/path/to/HarborRAG/docs/getting-started/installation.md'
[local] parser='markdown'
[local] chars=10959
[local] elements=70 warnings=0
```

Each provider also has its own script, such as `local.py` or `jira.py`, which takes no
arguments and needs exactly one enabled connection for its provider.

## Run every enabled connection

After single connections pass, run them all:

```bash
uv run python packages/harborrag-adapters/tests/connectors/smoke/run_all.py
```

`run_all.py` runs each enabled connection with its provider's runner. It exits `1` when
any connection does not pass, and a connection that is not configured (exit `2`) counts
as not passing. With the shipped file, a setup that fills in only `LOCAL_SOURCE_PATH`
therefore exits `1`; use `run.py --connector` for one connection instead. It exits `2`
when no enabled connection has a runner.

## What each check verifies

`run.py` discovers up to `--limit` records. A provider script or `run_all.py` uses that
provider's own limit.

| Provider | Limit without `run.py` | Passes when |
| --- | ---: | --- |
| Local | 5 | At least one record is found, and every discovered file loads and parses |
| GitHub | 3 | At least one repository file is found and the first one loads |
| Confluence | 10 | The first page loads without attachments; with `include_attachments: true`, every discovered page loads with them |
| Jira | 3 | The first issue loads without attachments; with `include_attachments: true`, every discovered issue loads with them |
| SharePoint | 10 | At least one item is found, and the first file and, with `include_pages: true`, the first page load |

The attachment pass runs only when the connection's `include_attachments` setting is
`true`; when it is `false`, the script says it skips the pass and still passes. In the
pass, Confluence and Jira fail when an attempted attachment ends as `failed` or
`unsupported`. A source without attachments can still pass.

Local fails on a parse error, or when the parser returns no text for a file that has
content. An empty or whitespace-only file passes, and so does an image in which OCR
finds no text.

## Save the parsed output

Nothing is written to disk by default. `--output txt` or `--output md` saves one file
per record in `packages/harborrag-adapters/tests/connectors/smoke/output/`;
`--output-dir` picks another folder. Local, Confluence and Jira support it; GitHub and
SharePoint exit `2` with `--output is not supported`.

```bash
uv run python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector jira-main --output md
```

- `txt` is the parsed text: for Confluence and Jira, the body followed by the text of
  each parsed attachment.
- `md` is a structured page: a title, a short header naming the source, a `## Metadata`
  section with the provider fields that have a value, and the body.
  - Confluence adds an `## Attachments` section with one `###` heading per parsed
    attachment.
  - Jira has no attachment section, because the connector already puts attachment text
    in the issue body; image attachments are listed under `## Attachment images`.
  - Local adds a `## Links` section for the links an HTML file contains.
- In `md` output, Confluence and Jira image attachments are saved in a
  `<output-file-stem>.assets/` folder beside the file and embedded from there. A local
  image file is embedded with a `file://` link to the original.
- Figures inside a local PDF appear under `## Figures` only when Docling parses it with
  its `image_output_dir` setting. The shipped LiteParse engine reports no figures.

## Parser engines and the OCR server

The scripts parse with the engines `config/ingestion/parsers.yaml` enables, PDFs and
images included, and print which engines they resolved. To try another engine, change
the enabled definition in that file; no variable overrides it. The file's settings,
including the OCR server, are described in
[Parser configuration](../../../../../docs/users/configuration/parser-config.md).

LiteParse sends scanned pages and images to the OCR server in
`HARBORRAG_OCR_SERVER_URL`, or OCRs them locally with Tesseract when it is empty. A
worker in a container reaches the server by a Docker name such as `ppocr-server`, which
does not resolve on the host. When the configured host name does not resolve, the
scripts keep the scheme, port and path, switch the host to `localhost`, and say so:

```text
[parsers] liteparse ocr_server_url='http://ppocr-server:8888/ocr' is not reachable from this host; using 'http://localhost:8888/ocr'
```

This works when the OCR server publishes the same port on the host. To point only the
smoke run at another OCR server, set `HARBOR_SMOKE_OCR_SERVER_URL`:

```bash
HARBOR_SMOKE_OCR_SERVER_URL=http://ocr.example.com:8888/ocr \
  uv run python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector jira-main
```

## Troubleshooting

The scripts print record IDs, media types, character counts and attachment statuses, not
content. `HARBOR_SMOKE_VERBOSE=1` adds short, redacted previews; the `CI` variable turns
them off again.

- Exit `2`: the printed message names the missing connection or variable. Check
  `config/sources/connectors.yaml` and `env/.env.connector`.
- No records: check that the source holds readable documents and that the repository,
  space, project, site or drive scope is right.
- Authentication errors: check the Cloud email, token scopes, Microsoft tenant and
  client IDs, VPN, proxy and provider URL.
- Attachment or parse failures: check the attachment's type and size,
  `config/ingestion/parsers.yaml` and that the engine it enables is installed.
- `OCR failed: ... error sending request`: LiteParse cannot reach its OCR server. Start
  the server, or set `HARBOR_SMOKE_OCR_SERVER_URL`.
