# Connector Configuration

The runtime loader reads versioned YAML containing named connector instances. Start from [`config/connectors.example.yaml`](../../../config/connectors.example.yaml).

## Supported providers

| Provider | Common aliases | Source |
| --- | --- | --- |
| `local` | `filesystem`, `files`, `local_files` | Local file or directory tree |
| `github` | `github_repo`, `github_repository` | GitHub repository |
| `confluence` | `confluence_cloud`, `confluence_datacenter` | Confluence space |
| `jira` | `jira_cloud`, `jira_datacenter` | Jira projects/issues |
| `sharepoint` | `microsoft_sharepoint`, `sharepoint_online` | SharePoint site, document library, folder, or files, plus modern site pages when `include_pages` is on |

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
      max_file_size_bytes: 104857600
```

Each key under `connectors` is an application-level name and the public
ingestion API `connection_id`. Multiple names may use the same provider. API
requests select a configured key; they never provide connector credentials or
repeat provider settings such as source scope, paths, comments, or attachments.
The request's `mode` controls unchanged-document admission, not connector
configuration; see [Ingestion modes](../ingestion-modes.md).

| Field | Meaning |
| --- | --- |
| `provider` | Required registered provider or alias |
| `enabled` | Included by `build_enabled()`; defaults to true |
| `settings` | Literal fields for the provider configuration dataclass |
| `environment` | Config-field to environment-variable mapping for non-secret values |
| `secrets` | `<config_field>_env: VARIABLE_NAME` references for secret values |

Unknown providers, fields, aliases, versions, and malformed mappings fail during loading. Provider-specific values are validated when a connector is built.

## Load and build

```python
from harborrag_runtime.config import load_connector_catalog

catalog = load_connector_catalog("config/connectors.example.yaml")
print(catalog.names(enabled_only=True))

local_docs = catalog.build(
    "harborrag-workspace",
    environment={"LOCAL_SOURCE_PATH": "docs"},
)
```

`catalog.build(name)` builds a definition even if disabled. `catalog.build_enabled()` builds only enabled definitions and returns a dictionary keyed by application-level name.

## Precedence and paths

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

Relative local `source_path` values written directly in YAML resolve against the YAML file's directory. An environment-backed relative path resolves against the process working directory. A code override for `source_path` also resolves from the process context.

### Select local content deliberately

The local connector treats every matching file as source content; it cannot
infer that a text file containing timestamps, ANSI escapes, or error lines is
"only a system log." Restrict discovery before ingestion so generated logs,
exports, caches, and build output never become canonical documents:

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

`allowed_extensions` is an allowlist. `excluded_extensions`, `exclude_paths`,
and `exclude_globs` remove candidates from that set. Filters affect future
discovery; removing an already published document still follows the normal
source reconciliation and cleanup lifecycle.

### Include SharePoint pages

By default the SharePoint connector reads only files from the site's document
library. Set `include_pages: true` to also ingest the site's modern pages:

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

Each page is loaded as one HTML document: the page title, then its text web
parts in the order they appear on the page. Other web parts (images, embedded
lists, news, quick links) are skipped. A page with no text still loads, with
only its title. Classic wiki pages are not supported.

With pages on, `.aspx` files in the drive walk are skipped, so a page stored in
the Site Pages library is never ingested twice. Discovery selectors that point
into a drive (`item_ids`, `root_item_id`, or a query `path`) return files only.
Pages use the same `Sites.Selected` grant as files.

### SharePoint crawl scope, allowlists, and skips

A SharePoint connection crawls exactly one of these scopes:

| Scope | Settings |
| --- | --- |
| Whole site (every document library) | `crawl_all_drives: true` |
| One document library | default drive, or `drive_id` / `drive_name` |
| One folder | `root_path` inside that library; subfolders are included unless `recursive: false` |
| Single files | `item_ids: [<drive item id>, ...]` inside that library |

`crawl_all_drives` cannot be combined with `drive_id`, `drive_name`,
`root_path`, or `item_ids`, and `item_ids` cannot be combined with `root_path`.

Optional allowlists narrow any scope further. An item must pass every allowlist
that is set, and it is checked before its content is downloaded:

```yaml
    settings:
      allowed_tenant_ids: [00000000-0000-0000-0000-000000000000]
      allowed_sites: [https://contoso.sharepoint.com/sites/Engineering]
      allowed_path_prefixes: [Policies, Runbooks/Production]
```

- `allowed_tenant_ids` is compared with the configured `tenant_id`. With a
  tenant allowlist but no known `tenant_id` (a raw `access_token`), nothing is
  crawled.
- `allowed_sites` accepts a site URL, a full Graph site ID, or one GUID part of
  it.
- `allowed_path_prefixes` are drive-relative folder paths. `/` allows the
  drive root. Folders that cannot contain an allowed path are not listed at
  all. Site pages are not in a drive and are not filtered by path.

`allowed_extensions` defaults to `docx, pdf, pptx, xlsx, txt, md`. Every entry
must be readable by a HarborRAG parser, and `max_file_size_bytes` must be
greater than 0. Invalid settings (a malformed path or URL, an unknown extension,
an empty scope list) fail when the connector is built.

Items the connector declines are logged and reported through the connector's
`skipped` list with one of these reasons: `NOT_IN_ALLOWLIST`,
`UNSUPPORTED_EXTENSION`, `EXCEEDS_SIZE_LIMIT`, `DELETED`, `ACCESS_DENIED`,
`TRANSIENT_ERROR_MAX_RETRIES`. A skipped item never stops the run. The run
stops only on invalid config, rejected credentials, or a site the app has not
been granted.

#### Permissions

The app registration must use the application permission `Sites.Selected`,
granted only for the configured sites. The connector refuses a token that
carries a broad role (`Sites.Read.All`, `Sites.ReadWrite.All`,
`Sites.Manage.All`, `Sites.FullControl.All`, `Files.Read.All`,
`Files.ReadWrite.All`) and fails at startup with an authentication error.

#### Delegated sign-in

When the tenant only allows delegated access, set `auth_mode: device_code`.
The connector then reads as a signed-in user, so it sees only what both the
app grant and that user can read. The app registration needs the delegated
permissions `Sites.Selected` and `offline_access` with admin consent, a Read
grant on the site, and **Allow public client flows** enabled. No client
secret is used.

```yaml
connectors:
  knowledge-sharepoint:
    provider: sharepoint
    environment:
      site_url: SHAREPOINT_SITE_URL
      tenant_id: MICROSOFT_TENANT_ID
      client_id: MICROSOFT_CLIENT_ID
    settings:
      auth_mode: device_code
      root_path: Policies
```

Sign in once on the machine that runs ingestion:

```bash
python -m harborrag_adapters.connectors.sharepoint.login \
  --tenant-id <tenant-guid> --client-id <app-guid>
```

The command prints a code to enter at `https://microsoft.com/devicelogin`,
then stores the refresh token in
`~/.harborrag/sharepoint/token-<tenant>-<client>.json` (override with
`token_cache_path` on both the connection and `--token-cache-path`). Each run
redeems it and stores the rotated token. If the token expires or access is
revoked, the run fails with an authentication error asking you to sign in
again. `--logout` deletes the stored sign-in. The broad-permission check above
also applies to delegated scopes.

`auth_mode` is otherwise inferred: `access_token` when a token is set, else
`client_credentials`. Delegated access is never inferred.

## Secrets and dynamic values

Keep credentials out of `settings`:

```yaml
secrets:
  token_env: GITHUB_TOKEN
```

Every referenced variable must exist and be non-empty when that connector is built. Disabled definitions do not need secrets when using `build_enabled()`.

Callbacks and custom parser objects are Python values and cannot be represented safely in this YAML boundary. Supply `custom_parsers`, attachment callbacks, or file callbacks in provider config code or explicit overrides.

## Operational limits

HTTP connectors validate timeouts, retry counts, backoff, page sizes, request rates, and source-specific collection/size limits. Configuration objects are construction recipes, not live mutable controls; construct a new connector after changing settings.

Use [Testing](../../developers/testing/README.md#real-system-smoke-checks) for credentialed connector checks.
