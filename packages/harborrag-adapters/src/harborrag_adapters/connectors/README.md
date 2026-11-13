# Connectors

A connector discovers the items of one kind of source system, such as a Jira project or
a folder of files, and loads each item's raw bytes and metadata. This page is for
contributors working in this folder: the connector contract, the metadata rules, the
provider modules and how to build a connector directly in Python.

Connectors do not parse content and do not schedule or parallelise ingestion. Parsers
and `harborrag-runtime` own those; see
[Architecture: adapter layer](../../../../../docs/developers/architecture/README.md#adapter-layer).

## The connector contract

Every connector subclasses `BaseConnector` in `base.py`. A provider implements two
methods:

- `discover(query)` yields cheap `SourceRecord` objects: stable IDs, locators,
  timestamps and checksums when the source has them, and compact provider metadata.
- `load(record)` performs the heavier fetch for one record and returns a `RawDocument`.

`BaseConnector` adds the rest:

| Member | What it does |
| --- | --- |
| `load_raw_documents(query, on_error=...)` | Discovers and loads in one pass. `on_error="raise"` (the default) stops at the first failure; `"skip"` logs and skips a failed record but still raises `AuthenticationError` |
| `skipped` | Items the last discovery dropped for a reportable reason, such as a size limit. Override it when a provider enforces such limits |
| `discover_page(query, cursor=..., page_size=...)` | One bounded page of discovery. The fallback replays `discover` with a numeric cursor; providers with native cursors override it |
| `describe(record)` | Admission descriptors for a record without fetching its bytes |
| `connect()` and `close()` | Optional eager connection check and resource cleanup; a connector is also a context manager |

`SourceRecord` and `RawDocument` live in `harborrag-core`, so runtime, engine and
adapters share one contract. The skeleton of a new connector class is in
[Extending HarborRAG: connectors](../../../../../docs/developers/extending/README.md#connectors).

## Metadata rules

- A loaded document's URL goes in `RawDocument.source` and its content type in
  `RawDocument.content_type`. Do not repeat either inside provider metadata.
- Every loaded metadata schema inherits `ConnectorMetadata` in `schemas.py`, which
  supplies the normalized `record_id`, `title`, `checksum`, `created_at` and
  `updated_at` fields. Provider schemas add source-specific fields.
- `source_system` and `metadata_schema_version` are class attributes of each provider
  schema, not constructor arguments. The inherited JSON-safe `to_dict()` adds them when
  it serializes metadata into `RawDocument.metadata`.
- Discovery timestamps and checksums live on `SourceRecord`. Loaded timestamps and
  checksums use the `ConnectorMetadata` fields, because the discovery record does not
  cross the raw-document boundary.

## Providers

| Provider | Connector | Config | Reads |
| --- | --- | --- | --- |
| `local` | `LocalFileConnector` | `LocalFileConfig` | Files under one local file or directory |
| `github` | `GitHubConnector` | `GitHubRepositoryConfig` | Repository files through the GitHub REST API |
| `confluence` | `ConfluenceConnector` | `ConfluenceSpaceConfig` | Confluence pages, optionally with comments and attachments |
| `jira` | `JiraConnector` | `JiraProjectConfig` | Jira issues, optionally with comments, changelog and attachments |
| `sharepoint` | `SharePointConnector` | `SharePointSiteConfig` | SharePoint drive items, and site pages when `include_pages` is on, through Microsoft Graph |

`__init__.py` registers each provider in `connector_registry` with one
`ConnectorProviderDefinition`, and `HarborConnector` builds any of them by name. The
aliases each provider accepts are listed in
[Connector configuration: supported providers](../../../../../docs/users/configuration/connector-config.md#supported-providers).

```python
from harborrag_adapters.connectors import HarborConnector, LocalFileConfig

connector = HarborConnector("local", config=LocalFileConfig(source_path="docs"))
```

### Provider document transforms

Source-specific canonical behaviour lives in the provider's `document_transform.py`. A
transform's `transform(raw, parsed, document)` takes the `RawDocument`, the parser's
`ParsedDocument` and the provider-neutral `Document` the engine built, and returns the
enriched `Document`. The `confluence`, `jira` and `local` providers have one, set as
`document_transform_factory` on their definition; providers without one use the generic
normalization. Runtime finds transforms through the registry, never through a
provider-name branch; see
[Extending HarborRAG: runtime services](../../../../../docs/developers/extending/README.md#runtime-services).

## Query shape

`ConnectorQuery` holds the filters every provider shares:

| Field | Default | Meaning |
| --- | --- | --- |
| `path` | `None` | Provider-specific root: a folder, project or source path |
| `pattern` | `None` | Provider-specific search string or glob-like pattern |
| `recursive` | `True` | Whether child folders, pages or items are traversed |
| `updated_after` | `None` | Incremental-sync timestamp filter, where the provider supports it |
| `limit` | `None` | Maximum number of records this discovery returns |
| `include_attachments` | `True` | Per-query attachment switch for providers that load attachments |
| `filters` | `{}` | Provider-specific selectors, such as IDs, labels, file paths or extensions |

```python
from harborrag_adapters.connectors import ConnectorQuery

recent_docs = ConnectorQuery(
    path="docs",
    recursive=True,
    filters={"extensions": [".md", ".pdf"]},
)

specific_github_files = ConnectorQuery(
    filters={"file_paths": ["README.md", "docs/architecture.md"]},
)

specific_jira_issues = ConnectorQuery(
    filters={"issue_keys": ["DEMO-1", "DEMO-2"]},
)
```

## Build a connector in Python

The runtime builds connections from the control database and from
`config/sources/connectors.yaml`; adapters never read configuration files. The file
format and the `harborrag_runtime.config` loader are described in
[Connector configuration](../../../../../docs/users/configuration/connector-config.md#load-and-build).

You can also construct a provider config directly. A config never reads credentials
from the process environment: pass each one as an argument. A GitHub config without a
token reads public repositories anonymously; a Jira, Confluence or SharePoint config
without credentials fails validation.

```python
import os

from harborrag_adapters.connectors import (
    ConfluenceConnector,
    ConfluenceSpaceConfig,
    GitHubConnector,
    GitHubRepositoryConfig,
    JiraConnector,
    JiraProjectConfig,
    LocalFileConfig,
    LocalFileConnector,
    SharePointConnector,
    SharePointSiteConfig,
)

local = LocalFileConnector(
    LocalFileConfig(
        source_path="docs",
        allowed_extensions={".md", ".txt", ".pdf"},
        follow_symlinks=False,
    )
)

github = GitHubConnector(
    GitHubRepositoryConfig(
        owner="example",
        repo="knowledge-base",
        branch="main",
        root_path="docs",
        token=None,  # anonymous: public repositories only
    )
)

confluence = ConfluenceConnector(
    ConfluenceSpaceConfig(
        space_key="DEMO",
        base_url="https://example.atlassian.net/wiki",
        include_comments=True,
        include_attachments=True,
        token=os.environ["CONFLUENCE_TOKEN"],
        email=os.environ["CONFLUENCE_EMAIL"],  # Cloud only
    )
)

jira = JiraConnector(
    JiraProjectConfig(
        base_url="https://example.atlassian.net",
        project_keys=["DEMO"],
        include_comments=True,
        include_changelog=True,
        token=os.environ["JIRA_TOKEN"],
        email=os.environ["JIRA_EMAIL"],  # Cloud only
    )
)

sharepoint = SharePointConnector(
    SharePointSiteConfig(
        site_url="https://example.sharepoint.com/sites/Knowledge",
        drive_name="Documents",
        root_path="Shared Documents",
        access_token=os.environ["MICROSOFT_GRAPH_TOKEN"],
    )
)
```

For app-only SharePoint sign-in, pass `tenant_id`, `client_id` and `client_secret`
instead of `access_token`. With `auth_mode="device_code"`, pass `tenant_id`,
`client_id` and either `token_cache_path` or `harborrag_tenant_id`; delegated access
uses only the signed-in user's token. The sign-in methods each source supports
are listed in
[Connections: pick the source type](../../../../../docs/users/connections.md#1-pick-the-source-type).

## Safety and scale

Connector safeguards are source-specific and deliberately conservative:

- HTTP connectors respect the configured request rate and retry transient status codes.
- Retry delays follow the `Retry-After` and `X-RateLimit-Reset` headers when a provider
  sends them (`policies/http.py`).
- Authenticated downloads must stay on the source's trusted origin.
- The local connector resolves its scope before loading. Symlinks are off by default
  and, when on, cannot escape the configured source root.
- File connectors check `max_file_size_bytes` before loading a payload, and attachment
  connectors check `max_attachment_size_bytes` before parsing one.
- Confluence and Jira cap nested comments, attachments and changelog entries.

Concurrency, global rate budgets, job checkpoints and resumable ingestion belong in
`harborrag-runtime`, not in a connector.

## Module layout

Every provider package has the same core modules:

```text
connectors/<provider>/
  __init__.py
  config.py       # provider configuration and validation
  connector.py    # discover and load orchestration
  mappers.py      # provider payloads to SourceRecord and metadata
  schemas.py      # typed provider metadata
```

Add other modules only when they remove real complexity from `connector.py`, and name
each one for the capability it implements. The current providers use:

| Module | Purpose | Used by |
| --- | --- | --- |
| `client.py` | HTTP, authentication and rate-limit client | `github`, `confluence`, `jira`, `sharepoint` |
| `discovery.py`, `query.py` | Discovery traversal and query building | `confluence`, `jira` |
| `content.py` | Content traversal or text rendering | `confluence`, `jira` |
| `document_transform.py` | Canonical document enrichment | `confluence`, `jira`, `local` |
| `filters.py` | Query and config filter normalization | `github`, `local` |
| `issues.py` | Jira issue search and nested pagination | `jira` |
| `repository.py`, `repository_paths.py` | Repository traversal, blobs, URLs and paths | `github` |
| `drive.py`, `pages.py`, `login.py` | Drive traversal, site pages and delegated sign-in | `sharepoint` |
| `filesystem.py` | Filesystem traversal and filtering | `local` |

Code shared by several providers lives beside them: `atlassian/` for the Jira and
Confluence client and query helpers, `attachments/` for attachment selection, loading
and parsing, and `policies/` for HTTP, network and validation rules.

## Add a connector

[Extending HarborRAG: connectors](../../../../../docs/developers/extending/README.md#connectors)
lists what a new connector ships with and how to register it. Inside this folder:

- Keep authentication and SDK imports in `client.py` or the provider module.
- Put schema-to-domain conversion in `mappers.py` and validate config in `config.py`.
- Inherit loaded metadata from `ConnectorMetadata`, following the
  [metadata rules](#metadata-rules).
- Return `SourceRecord` from discovery and `RawDocument` from `load`.
- Raise the shared exceptions in `exceptions.py`, such as `AuthenticationError`,
  `FetchError` and `RateLimitError`.
- Add provider tests under `packages/harborrag-adapters/tests/connectors/`, with fakes
  rather than a production `mock` connector.

## Logging

Each provider logs under its own logger name:

```text
harborrag.adapters.connectors.<provider>
```

Logs name the source, route, retry, skip reason and failure boundary, and never contain
tokens, secrets or full authenticated URLs.
