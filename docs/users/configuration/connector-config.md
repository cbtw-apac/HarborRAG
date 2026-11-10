# Connector Configuration

`config/sources/connectors.yaml` lists the data sources HarborRAG ingests when it
starts for the first time: a folder of local files, a GitHub repository, a Confluence
space, Jira projects or a SharePoint site. This page describes the file's format and
the options of each provider, the kind of system a connector reads. After the first
start, you change the same sources as connections through the API or
`harborrag source`; see [Connections](../connections.md).

The repository ships two versions of the file in `config/sources/`:

- [`connectors.yaml`](../../../config/sources/connectors.yaml) declares local files,
  Confluence, Jira and SharePoint.
- [`connectors.example.yaml`](../../../config/sources/connectors.example.yaml) holds
  only the local-files entry, as a starting point.

Schedules for these sources live beside them in `config/sources/schedules.yaml`; see
[Scheduled Ingestion](../scheduled-ingestion.md).

## When HarborRAG reads this file

A connection is one configured data source, stored in the control database, the
database where HarborRAG keeps its own state such as connections, runs and settings.
HarborRAG turns this file into connections once:

- The API imports it at its first start, and the CLI before its first ingestion. The
  import runs only while the install holds no project and no connection in any tenant.
  A failed import is recorded and never retried.
- Each entry becomes a connection in project `default` of the tenant
  `HARBORRAG_TENANT_ID` names (`DEFAULT` unless set). The entry's key becomes the
  connection's `source_key`, the name you pass to `harborrag ingest run`. It is also the
  `connection_id` when it is a valid id; otherwise the id is a slug of the key.
- Entries with `enabled: false` are skipped.
- Values named under `environment:` are read once, at the import, and stored with the
  connection. Tokens and passwords named under `secrets:` are stored as references to
  their variable, which the process running the ingestion reads at the start of every
  run, so a new token takes effect without another import.
- An entry whose variable is unset is still imported, with status `error`. Fix it with
  `harborrag source update` or the API; see
  [Connection status](../connections.md#connection-status).

After the import, the service ignores edits to this file. `harborrag doctor` and the
Python loader below still read it.

## Supported providers

| Provider | Aliases | API `source_type` | Reads |
| --- | --- | --- | --- |
| `local` | `filesystem`, `files`, `local_files` | `local_file` | A local file or directory tree |
| `github` | `github_repo`, `github_repository` | `github` | A GitHub repository |
| `confluence` | `confluence_cloud`, `confluence_datacenter` | `confluence` | A Confluence space |
| `jira` | `jira_cloud`, `jira_datacenter` | `jira` | Jira projects and their issues |
| `sharepoint` | `microsoft_sharepoint`, `sharepoint_online` | `sharepoint` | A SharePoint site, document library, folder or files, plus modern site pages when `include_pages` is on |

## File shape

```yaml
version: 1

connectors:
  harborrag-workspace:
    provider: local
    enabled: true
    environment:
      source_path: LOCAL_SOURCE_PATH
    settings:
      allowed_extensions: [md, txt, pdf]
      include_hidden: false
      follow_symlinks: false
      max_file_size_bytes: 20971520
```

Each key under `connectors` names one connection. Several keys may use the same
provider.

| Field | Meaning |
| --- | --- |
| `provider` | Required; a provider or alias from the table above |
| `enabled` | `false` skips the entry at import and in `build_enabled()`; defaults to `true` |
| `settings` | Literal values of the provider's options |
| `environment` | Option name to environment variable, for values that are not secret, such as URLs and keys |
| `secrets` | `<option>_env: VARIABLE` references for credentials |

Unknown providers, options, aliases, versions and malformed mappings fail when the
file loads. Provider-specific values, such as a URL that is not `https://`, are
checked when the connector is built.

Keep credentials out of `settings`; name their variable under `secrets` instead:

```yaml
secrets:
  token_env: GITHUB_TOKEN
```

Callbacks and custom parser objects are Python values that this YAML cannot express.
Pass `custom_parsers`, attachment callbacks or file callbacks as code overrides; see
[Load and build](#load-and-build).

## Local files

The `local` provider reads every file under `source_path` that passes its filters.
It cannot tell that a text file full of timestamps and error lines is "only a log", so
restrict discovery before the first ingestion. Otherwise generated logs, exports,
caches and build output become documents:

```yaml
connectors:
  harborrag-workspace:
    provider: local
    enabled: true
    environment:
      source_path: LOCAL_SOURCE_PATH
    settings:
      allowed_extensions: [md, markdown, txt, pdf]
      excluded_extensions: [log]
      exclude_paths: [logs, build, dist]
      exclude_globs: ["*.log", "**/*.log", "**/logs/**", "**/*.jsonl"]
      include_hidden: false
```

`allowed_extensions` is an allowlist. `excluded_extensions`, `exclude_paths` and
`exclude_globs` remove candidates from it. Filters apply to future discovery; a
document that is already published leaves through the normal source reconciliation
and cleanup.

A relative `source_path` written in the YAML resolves against the YAML file's folder.
A relative path read from an environment variable, or passed as a code override,
resolves against the process's working directory.

## Remote providers

Remote providers take URLs and source identifiers from environment variables, and
credentials from `secrets`. The variables go in `env/.env.connector` of your
repository checkout. `harborrag doctor`
fails its `connector credentials` check and names every blank variable an enabled
remote entry needs.

## GitHub

```yaml
connectors:
  engineering-github:
    provider: github
    environment:
      repository_url: GITHUB_REPOSITORY_URL
    settings:
      branch: main
      root_path: docs
      allowed_extensions: [md, txt, pdf]
    secrets:
      token_env: GITHUB_TOKEN
```

The provider reads the owner and repository name from `repository_url`. A private
repository needs a token; GitHub explains how to create one in
[Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).
A public repository works without one, within GitHub's limit for unauthenticated
requests ([Connections](../connections.md#add-a-connection)).

## Jira and Confluence

The Jira provider reads the issues of `project_keys`; the Confluence provider reads
the pages of `space_key`. The shipped file takes both from the environment, and
`JIRA_PROJECT_KEY` may hold several keys separated by commas.

| Setting | Provider | Meaning |
| --- | --- | --- |
| `base_url` | both | Root URL of the site, `https://` |
| `project_keys` | Jira | Projects to read |
| `space_key` | Confluence | Space to read |
| `content_types` | Confluence | `page`, `blogpost` or both; default `[page]` |
| `deployment_type` | both | `cloud` or `datacenter`; when left out, a host ending in `.atlassian.net` means `cloud` |
| `include_comments` | both | Read comments; default `true` for Jira, `false` for Confluence |
| `include_attachments` | both | Read attachments as documents of their own; default `false` |

`GET /v1/admin/source-types` lists every other option with its default; see
[Add a connection](../connections.md#add-a-connection).

`deployment_type` decides how the connector signs in, which the sign-in table in
[Add a connection](../connections.md#add-a-connection) lists as `api_token` (Cloud)
and `pat` (Data Center).

<details markdown="1"><summary>Jira Cloud or Confluence Cloud</summary>

Cloud signs in with an account's email address and an Atlassian API token. Create the
token as Atlassian describes in
[Manage API tokens for your Atlassian account](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/).

```yaml
connectors:
  jira-main:
    provider: jira
    environment:
      base_url: JIRA_BASE_URL
      project_keys: JIRA_PROJECT_KEY
    settings:
      deployment_type: cloud
      include_comments: true
      include_attachments: false
    secrets:
      token_env: JIRA_TOKEN
      email_env: JIRA_EMAIL
```

For Confluence, use `provider: confluence`, `space_key: CONFLUENCE_SPACE_KEY` in
place of `project_keys`, and `CONFLUENCE_TOKEN` and `CONFLUENCE_EMAIL`.

</details>

<details markdown="1"><summary>Jira Data Center or Confluence Data Center</summary>

Data Center signs in with a personal access token and no email address. Create the
token as Atlassian describes in
[Using personal access tokens](https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html).

```yaml
connectors:
  confluence-dc:
    provider: confluence
    environment:
      base_url: CONFLUENCE_BASE_URL
      space_key: CONFLUENCE_SPACE_KEY
    settings:
      deployment_type: datacenter
      content_types: [page, blogpost]
    secrets:
      token_env: CONFLUENCE_TOKEN
```

Connectors reach only public addresses by default. A server inside your network must
be listed in `HARBORRAG_CONNECTOR_ALLOWED_HOSTS`; see
[Network access](../connections.md#network-access).

</details>

The Jira connector reads `JIRA_TOKEN`, the variable `token_env` names. It falls back to
`JIRA_API_TOKEN` only when you build the connector yourself in Python without a token.

### Filter Jira and Confluence attachments by type

```yaml
connectors:
  jira-main:
    provider: jira
    settings:
      include_attachments: true
      allowed_extensions: [pdf, docx]
      excluded_extensions: [exe]
```

The filters apply to attachments only; issues and pages are always read. An excluded
type wins over an allowed one, and `max_attachment_size_bytes` defaults to 20 MiB.
[File type filters](../connections.md#file-type-filters) gives the default type list
and how a type is recognised.

## SharePoint

The SharePoint provider reads files from a site's document libraries through
Microsoft Graph, and optionally the site's modern pages. It signs in as an app
registered in Microsoft Entra ID; Microsoft explains how to create one in
[Register an application](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app).
`graph_api_url` defaults to `https://graph.microsoft.com/v1.0` and differs only for
national clouds.

`auth_mode` selects how the connector gets its Graph token. When it is left out, it
is `access_token` if a token is set, else `client_credentials`; delegated access is
never inferred.

<details markdown="1"><summary>App-only sign-in (client_credentials)</summary>

The app signs in with its own client secret and an application `Sites.Selected` grant
([Permissions](#permissions)).

```yaml
connectors:
  knowledge-sharepoint:
    provider: sharepoint
    environment:
      site_url: SHAREPOINT_SITE_URL
    settings:
      drive_name: Documents
    secrets:
      tenant_id_env: MICROSOFT_TENANT_ID
      client_id_env: MICROSOFT_CLIENT_ID
      client_secret_env: MICROSOFT_CLIENT_SECRET
```

</details>

<details markdown="1"><summary>Delegated sign-in (device_code)</summary>

The connector reads as a user who signed in once; no client secret is used. Set up
the app and sign in as [Delegated sign-in](#delegated-sign-in) describes.

```yaml
connectors:
  knowledge-sharepoint:
    provider: sharepoint
    environment:
      site_url: SHAREPOINT_SITE_URL
    settings:
      auth_mode: device_code
      root_path: Policies
    secrets:
      tenant_id_env: MICROSOFT_TENANT_ID
      client_id_env: MICROSOFT_CLIENT_ID
```

`tenant_id` and `client_id` are not secret; listing them under `environment:` as
`tenant_id: MICROSOFT_TENANT_ID` loads the same way.

</details>

### Permissions

The app registration must use the `Sites.Selected` permission, granted only for the
sites it should read; Microsoft explains the grant in
[Selected permissions in OneDrive and SharePoint](https://learn.microsoft.com/en-us/graph/permissions-selected-overview).
The connector refuses a token that carries a broader role (`Sites.Read.All`,
`Sites.ReadWrite.All`, `Sites.Manage.All`, `Sites.FullControl.All`,
`Files.Read.All` or `Files.ReadWrite.All`) and fails at startup with an
authentication error. The check applies to delegated scopes too.

### Delegated sign-in

Use `auth_mode: device_code` when the Microsoft tenant allows only delegated access.
The connector then reads as a signed-in user, so it sees only what both the app grant
and that user can read. The app registration needs the delegated permissions
`Sites.Selected` and `offline_access` with admin consent, a Read grant on the site,
and **Allow public client flows** enabled. No client secret is used. Microsoft
describes the flow in
[OAuth 2.0 device authorization grant](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code).

Before the first run, sign in once on the machine that runs ingestion:

```bash
uv run python -m harborrag_adapters.connectors.sharepoint.login \
  --tenant-id <tenant-guid> --client-id <app-guid>
```

The IDs default to `MICROSOFT_TENANT_ID` and `MICROSOFT_CLIENT_ID`. When they are not
exported, the command reads those two values, and nothing else, from
`env/.env.connector` (or `$CONNECTOR_ENV_FILE`) relative to the current directory, so
from a checkout the command without arguments is enough.

The command prints a code to enter at `https://microsoft.com/devicelogin`, then stores
the refresh token in
`~/.harborrag/sharepoint/<harborrag-tenant>/token-<tenant>-<client>.json`, in the folder
of the HarborRAG tenant the connection belongs to, so two tenants never share a sign-in.
The tenant comes from `--harborrag-tenant`, else `HARBORRAG_TENANT_ID`, else `DEFAULT`.
To use another file, set `token_cache_path` on the connection and pass the same path as
`--token-cache-path`. Each run redeems the token and stores the rotated one. If the
token expires or access is revoked, the run fails with an authentication error asking
you to sign in again. `--logout` deletes the stored sign-in.

That sign-in serves ingestion that runs on your machine (`harborrag ingest run`). The
Temporal worker (`harborrag ingest start`) runs in a container that cannot read your
home folder, so sign it in separately:

```bash
scripts/deployment/dev.sh sharepoint-login
```

Add `--device gpu` for the GPU worker. The command runs the same sign-in inside the
worker image and keeps the token in the `harborrag-sharepoint-tokens` Docker volume,
which every worker replica reads and rotates. `dev.sh sharepoint-login --logout`
removes it, and so does `dev.sh down --volumes`. Rebuild the worker image first
(`dev.sh worker --build`) if it predates this command.

### Include SharePoint pages

By default the connector reads only files from the site's document library. Set
`include_pages: true` to also ingest the site's modern pages:

```yaml
connectors:
  knowledge-sharepoint:
    provider: sharepoint
    environment:
      site_url: SHAREPOINT_SITE_URL
    settings:
      drive_name: Documents
      include_pages: true
```

Each page is loaded as one HTML document: the page title, then its text web parts in
the order they appear on the page. Other web parts (images, embedded lists, news,
quick links) are skipped. A page with no text still loads, with only its title.
Classic wiki pages are not supported.

With pages on, `.aspx` files in the drive walk are skipped, so a page stored in the
Site Pages library is never ingested twice. Discovery selectors that point into a
drive (`item_ids`, `root_item_id`, or a query `path`) return files only. Pages use the
same `Sites.Selected` grant as files.

### SharePoint crawl scope, allowlists, and skips

A SharePoint connection crawls exactly one of these scopes:

| Scope | Settings |
| --- | --- |
| Whole site (every document library) | `crawl_all_drives: true` |
| One document library | default drive, or `drive_id` / `drive_name` |
| One folder | `root_path` inside that library; subfolders are included unless `recursive: false` |
| Single files | `item_ids: [<drive item id>, ...]` inside that library |

`crawl_all_drives` cannot be combined with `drive_id`, `drive_name`, `root_path` or
`item_ids`, and `item_ids` cannot be combined with `root_path`.

Optional allowlists narrow any scope further. An item must pass every allowlist that
is set, and it is checked before its content is downloaded:

```yaml
    settings:
      allowed_tenant_ids: [00000000-0000-0000-0000-000000000000]
      allowed_sites: [https://contoso.sharepoint.com/sites/Engineering]
      allowed_path_prefixes: [Policies, Runbooks/Production]
```

- `allowed_tenant_ids` is compared with the configured `tenant_id`. With a tenant
  allowlist but no known `tenant_id` (a raw `access_token`), nothing is crawled.
- `allowed_sites` accepts a site URL, a full Graph site ID, or one GUID part of it.
- `allowed_path_prefixes` are drive-relative folder paths. `/` allows the drive root.
  Folders that cannot contain an allowed path are not listed at all. Site pages are
  not in a drive and are not filtered by path.

`allowed_extensions` defaults to the shared list `md, markdown, txt, pdf, docx, doc,
epub, odt, pptx, csv, xlsx, png, jpg, jpeg, html`, and `max_file_size_bytes` to
20 MiB. Every entry must be readable by a HarborRAG parser, and `max_file_size_bytes`
must be greater than 0. Invalid settings (a malformed path or URL, an unknown
extension, an empty scope list) fail when the connector is built.

Items the connector declines are logged and reported in the connector's `skipped`
list with one of these reasons: `NOT_IN_ALLOWLIST`, `UNSUPPORTED_EXTENSION`,
`EXCEEDS_SIZE_LIMIT`, `DELETED`, `ACCESS_DENIED`, `TRANSIENT_ERROR_MAX_RETRIES`. A
skipped item never stops the run. The run stops only on invalid config, rejected
credentials, or a site the app has not been granted.

On HTTP 429 or 5xx the connector retries up to `max_retries` times, waiting for
`Retry-After` when Graph sends it and otherwise backing off exponentially with
jitter. When `Retry-After` asks for more than 300 seconds, the connector does not
retry early; it skips the item as `TRANSIENT_ERROR_MAX_RETRIES`, and the next run
picks it up.

## Large runs

A Jira project of 15,000 issues with attachments discovers a few hundred thousand
documents. Discovery is checkpointed per provider page and resumes after a failure,
and each document workflow reads only the plan page that holds it, so the run itself
is bounded. What decides whether it finishes is outside the code:

- **Rehearse with a limit.** Run the same scope first with `--limit` on
  `harborrag ingest run|start`, which stops after that many discovered artifacts. It
  gives the real documents-per-issue ratio, the attachment size distribution, the
  failure classes (for example Word 6.0/95 `.doc` files, which are rejected as
  unsupported) and the throughput of *your* host. Extrapolate from that, not from the
  issue count.
- **Throughput is CPU-bound.** OCR of image attachments dominates; on a 4-vCPU host
  with three workers expect roughly half a document per second, which is days for
  400,000 documents. A first pass with `include_attachments: false` finishes in hours
  and a second pass adds the attachments.
- **Credentials must outlive the run.** A token that expires mid-run fails discovery
  after its retries. Use a service credential that does not rotate during the run.
- **Disk.** Every attachment is kept as an immutable artifact (up to
  `max_attachment_size_bytes` each). Size the object store from the rehearsal.
- **Why did it stop?** `harborrag ingest status RUN_ID` and the run's event log record
  pause, resume and cancel requests with who asked (`task.<id>.control` events, stored
  but not sent on the HTTP event stream, where the new status appears as
  `run.status`). A run that ends with `cancelled_at_safe_boundary` was cancelled on
  request; it was not a timeout.

HTTP connectors validate timeouts, retry counts, backoff, page sizes, request rates
and source-specific collection and size limits. A connector's configuration is read
when it is built, so build a new connector after changing settings. For credentialed
checks against real systems, see
[Real-system smoke checks](../../developers/testing/README.md#real-system-smoke-checks).

## Load and build

```python
from harborrag_runtime.config import load_connector_catalog

catalog = load_connector_catalog("config/sources/connectors.example.yaml")
print(catalog.names(enabled_only=True))

local_docs = catalog.build(
    "harborrag-workspace",
    environment={"LOCAL_SOURCE_PATH": "docs"},
)
```

`catalog.build(name)` builds a definition even if it is disabled.
`catalog.build_enabled()` builds only enabled definitions and returns a dictionary
keyed by name; disabled definitions need no secrets there. Every variable an entry
references must exist and be non-empty when that entry is built.

Effective provider settings use this precedence:

```text
provider defaults < YAML settings < referenced environment < code overrides
```

```python
github = catalog.build(
    "engineering-github",
    overrides={"branch": "release", "requests_per_minute": 60},
)
```
