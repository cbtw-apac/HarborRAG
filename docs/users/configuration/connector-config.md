# Connector Configuration

The runtime loader reads versioned YAML containing named connector instances. Start from [`config/connectors.example.yaml`](../../../config/connectors.example.yaml).

## Supported providers

| Provider | Common aliases | Source |
| --- | --- | --- |
| `local` | `filesystem`, `files`, `local_files` | Local file or directory tree |
| `github` | `github_repo`, `github_repository` | GitHub repository |
| `confluence` | `confluence_cloud`, `confluence_datacenter` | Confluence space |
| `jira` | `jira_cloud`, `jira_datacenter` | Jira projects/issues |
| `sharepoint` | `microsoft_sharepoint`, `sharepoint_online` | SharePoint document library |

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
| `tenants` | Optional list of tenant IDs allowed to submit ingestion through this connection and to see it in `GET /v1/connections`. Omit it to allow every tenant (single-tenant default). `'*'` is rejected. See [Tenant allowlist](#tenant-allowlist). |

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

## Tenant allowlist

By default every tenant may submit ingestion through every connection, which
keeps single-tenant setups unchanged. In a multi-tenant deployment, bind each
connection to the tenants that may use it:

```yaml
connectors:
  acme-jira:
    provider: jira
    tenants: [acme]
```

The rule is enforced when a submission is prepared, so it covers the v1
`POST /v1/ingestions` route, the legacy `POST /api/v1/ingestions` route, and the
CLI. A tenant submitting a connection it is not allowed to use gets the same
"unknown connector" outcome as for a connection that does not exist (and for one
whose definition is invalid), so a response never reveals other tenants'
connections. Submission errors name only the requested connection; the
operator-facing catalog lookup still lists the configured connection IDs.

A disabled connection (`enabled: false`) is reported exactly like an unknown
one: `404` with `Connection was not found` on both submission routes and on
retry. Any other problem building the connection - a missing secret, invalid
provider settings, an unreadable catalog - returns the opaque configuration
error (`500`, `Connector configuration is unavailable`); the detail is written
only to the API log.

The allowlist is checked when a submission or retry is accepted. A submission
that was accepted but not yet started (for example while Temporal was
unavailable) is started later by the API's recovery loop without checking the
allowlist again, so removing a tenant from `tenants` does not withdraw work that
tenant already queued. Check that tenant's pending tasks with
`GET /v1/ingestions` before narrowing the allowlist.

`tenants` must be a non-empty list of tenant IDs without surrounding
whitespace; `'*'` is rejected (omit the field to allow every tenant). It is an
access rule, not connector configuration, so it does not change the source
fingerprint. Once any enabled connection declares `tenants`, the API logs a
startup warning naming each enabled connection that does not.

## Source access control

Jira, Confluence and SharePoint connectors check what the source says about who
may read each item, relative to the connector's configured scope (Jira project,
Confluence space, SharePoint library):

| Result | Meaning | `restricted_content: skip` (default) | `restricted_content: include` |
| --- | --- | --- | --- |
| inherited | Readable by everyone who can read the scope | ingested | ingested |
| restricted | Narrower than the scope | withheld, skip reason `access_restricted` | ingested with `metadata.source_acl` |
| unknown | Access could not be determined | withheld, skip reason `access_unresolved` | withheld, skip reason `access_unresolved` |

Withheld items are left out of discovery, so copies ingested earlier are retired,
but not at once: an item is retired only after `missing_threshold` consecutive
completed full scans miss it (default 2), and it stays served until then. Direct
loads and explicit-ID describes of a withheld item raise `SourceRestrictedError`. Jira and Confluence attachment loads first re-read the
parent issue or page and are refused the same way when the parent is withheld.

Use `include` only when `HARBORRAG_CORPUS_ACCESS_MODE=source_acl` and you import a
document snapshot for every restricted document (see `harborrag topology indexing
permissions-import`). `tenant_shared` serves every active document in the tenant
and ignores snapshots, so the runtime refuses to build a connector that sets
`restricted_content: include` unless the corpus access mode is `source_acl`; the
error names the connector and the other connectors keep working.

Even then, `include` does not fully hide a restricted document. Graph source-scope
entity nodes (each item's title and attributes) are gated only by the source-scope
snapshot, never by the document snapshot. A reader of the scope who is not in a
restricted document's snapshot can still see that document's title and attributes
through the graph tools. Use `skip` when titles or attributes are themselves
sensitive.

Restriction changes usually do not change an item's version, so incremental syncs
do not notice them. After tightening access at the source, run `missing_threshold`
full scans (default 2) to retire the newly withheld items.

For `include` mode, each restricted document needs a resolved permission snapshot
imported with `harborrag topology indexing permissions-import`. HarborRAG no longer
ships a helper that builds those snapshots from `source_acl`, so build them with
your own tooling for now. Class-based access, where HarborRAG computes reader sets
per ACL class, is meant to replace this manual step in a later release.

Your tooling reads the captured evidence from the discovered-item descriptor that
discovery records for each `document_id` (the serialized source record's
`metadata.source_acl`) and from the raw document's metadata. It is recorded for
every item that is ingested, inherited ones included, with:

- `realm`: the identity namespace of the principals. `atlassian:<host>` for Jira and
  Confluence; `entra:<tenant id>` for SharePoint. A SharePoint connector with no
  tenant id configured uses the placeholder `sharepoint:<host>`, which no Entra
  directory resolves.
- `scope`: what the ACL is relative to (`jira-project:<KEY>`,
  `confluence-space:<KEY>`, `sharepoint-drive:<drive id>`). `inherited` means exactly
  the readers of that scope.
- `status`, `reason` and `levels`: a reader must match at least one principal in
  every level. An `unknown` ACL is never imported.

Principals are keyed `<realm>|<kind>:<id>` and use the identifier the source
returned. Confluence Cloud groups are keyed by group id, Data Center groups by name.
For example:

- Jira security level 10100 on an issue of project `CPM`:
  `atlassian:acme.atlassian.net|jira_security_level:CPM/10100`. A security level
  is keyed within the issue's project (the same key as its `jira-project:<KEY>`
  scope), because one security scheme can serve several projects and a level's
  project-role and project-lead members resolve in the issue's project.
- Confluence Cloud group: `atlassian:acme.atlassian.net|group:<group id>`
- SharePoint site group 3 (tenant `t1`):
  `entra:t1|site_group:<site id>/3`, where `<site id>` is the Graph site id
  (`contoso.sharepoint.com,<site guid>,<web guid>`). SharePoint site users and site
  groups are only numbered within one site, so they carry the site; Entra `user` and
  `group` ids are tenant-wide and do not.

| Provider | Restriction captured | API access needed |
| --- | --- | --- |
| Jira | Issue security level (`fields.security`); comments with `visibility` or `jsdPublic: false`, and worklogs with `visibility`, are always dropped from the issue | Browse projects. The credential must be able to see the issues. Cloud OAuth: `read:jira-work` |
| Confluence | Page read restrictions plus every ancestor's (inherited view restrictions); space permissions are the source scope | The credential must view the page and every ancestor. Cloud OAuth: `read:confluence-content.permission` (verify for your app type). An ancestor the credential cannot read makes the page `unknown`. Ancestor restrictions are cached for one scan; a failed read is never cached |
| SharePoint | Item permissions compared with the library root (`/drives/{id}/items/{id}/permissions`, `/drives/{id}/root/permissions`); sharing links are ignored because they only widen access | Graph application permission `Sites.Read.All` or `Files.Read.All` (verify; `Sites.Selected` with a read grant on the site also works). One extra Graph request per file. Results are cached for one scan only; the next scan re-reads every permission, so a narrowed item is reclassified even when its `cTag` did not change. Site groups cannot be expanded through Graph: map the `site_group` principals (`<site id>/<id>`) in your own tooling. A 403 on a permissions read makes that file `unknown`; a 401 aborts the run. Explicit-ID restriction is enforced at load, because describe fetches nothing from SharePoint |

### Network policy

Jira, Confluence, GitHub and SharePoint base URLs must be HTTPS, must carry no
credentials, and must resolve only to public addresses. Loopback, private
(RFC 1918 and ULA), link-local (including 169.254.169.254) and other non-public
addresses are rejected when the configuration is built.

- DNS names are resolved at that point. A name that does not resolve is accepted,
  and the connection fails later.
- For an on-premises Data Center or GitHub Enterprise server on a private network,
  set `allow_private_network: true` on that connector only. HTTPS is still required.
- Hosts are checked when the configuration is validated, not on every connection,
  so DNS changes after that point are not re-checked.

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

### Large runs (tens of thousands of issues, attachments included)

A Jira project of 15,000 issues with attachments discovers a few hundred
thousand documents. Discovery is checkpointed per provider page and resumes
after a failure, and each document workflow reads only the plan page that
holds it, so the run itself is bounded. What decides whether it finishes is
outside the code:

- **Rehearse with `limit`.** Run the same scope with `query.limit` of a few
  thousand issues first. It gives the real documents-per-issue ratio, the
  attachment size distribution, the failure classes (for example legacy `.doc`
  files, which have no parser) and the throughput of *your* host. Extrapolate
  from that, not from the issue count.
- **Throughput is CPU-bound.** OCR of image attachments dominates; on a
  4-vCPU host with three workers expect roughly half a document per second,
  which is days for 400,000 documents. A first pass with
  `include_attachments: false` finishes in hours and a second pass adds the
  attachments.
- **Credentials must outlive the run.** A token that expires mid-run fails
  discovery after its retries. Use a service credential that does not rotate
  during the run.
- **Disk.** Every attachment is kept as an immutable artifact (up to
  `max_attachment_size_bytes` each). Size the object store from the rehearsal.
- **Why did it stop?** `harborrag ingest status RUN_ID` and the run's event
  trail record pause, resume and cancel requests with who asked
  (`task.<id>.control` events). A run that ends with
  `cancelled_at_safe_boundary` was cancelled on request; it was not a timeout.

