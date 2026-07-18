# Connector Configuration

The repository example at `config/connectors.yaml` defines named connector
instances. The loader is owned by `harborrag-runtime`; the existing provider
config dataclasses in `harborrag-adapters` remain the source of defaults,
normalization, and provider-specific validation.

## File format

```yaml
version: 1

connectors:
  engineering-github:
    provider: github
    enabled: true
    environment:
      repository_url: GITHUB_REPOSITORY_URL
    settings:
      branch: main
      root_path: docs
      allowed_extensions: [md, txt, pdf]
      requests_per_minute: 120
      request_timeout_seconds: 30
      max_retries: 3
      backoff_factor: 0.5
    secrets:
      token_env: GITHUB_TOKEN
```

Each key under `connectors` is an application-level source name. Multiple
entries can use the same provider, which allows separate repositories,
projects, sites, spaces, or directories to have independent settings.

The supported definition fields are:

| Field | Required | Meaning |
|---|---:|---|
| `provider` | Yes | `local`, `github`, `confluence`, `jira`, or `sharepoint`. Registered provider aliases are also accepted. |
| `enabled` | No | Whether `build_enabled()` constructs the connector. Defaults to `true`. |
| `settings` | No | Fields accepted by the provider's config dataclass. Unknown fields are rejected. |
| `environment` | No | Non-secret provider fields mapped to environment variable names, such as `base_url: CONFLUENCE_BASE_URL`. |
| `secrets` | No | Environment references written as `<config_field>_env: ENVIRONMENT_VARIABLE`. |

The top-level `version` is required. Only version `1` is currently supported.

## Loading connectors

```python
from harborrag_runtime.config import load_connector_catalog

catalog = load_connector_catalog("config/connectors.yaml")

# Build one connector, even if its definition is disabled.
local_docs = catalog.build("local-docs")

# Build only definitions with enabled: true.
enabled_connectors = catalog.build_enabled()
```

Loading validates the YAML structure, provider names, fields, and environment
references. Building resolves environment variables and constructs the normal
provider config dataclass, so provider validation happens before a connector
can make a request.

Explicit code overrides take precedence over YAML and resolved environment
values:

```python
github = catalog.build(
    "engineering-github",
    overrides={"branch": "release", "requests_per_minute": 60},
)
```

## URLs, paths, and secrets

Keep deployment-specific URLs and paths in `.env.connector.example` and map
them under `environment` using exact provider field names:

```yaml
environment:
  base_url: CONFLUENCE_BASE_URL
  space_key: CONFLUENCE_SPACE_KEY
```

The repository keeps only `local-docs` active. GitHub, Confluence, JIRA, and
SharePoint are complete commented examples. Uncomment any remote blocks you
need and the matching variables in `.env.connector.example`.

Do not store tokens, access tokens, or client secrets in `settings`. Reference
their environment variable names under `secrets`:

```yaml
secrets:
  tenant_id_env: MICROSOFT_TENANT_ID
  client_id_env: MICROSOFT_CLIENT_ID
  client_secret_env: MICROSOFT_CLIENT_SECRET
```

An explicitly referenced variable must exist and must not be empty when the
connector is built. Disabled definitions are not built by `build_enabled()`, so
their secrets do not need to be present.

Every referenced URL, setting, or secret must exist and be non-empty when its
connector is built. The effective precedence is:

```text
provider defaults < YAML settings < resolved environment < code overrides
```

## Paths and programmable settings

Relative `source_path` values written directly in YAML are resolved relative to
the directory containing the YAML file. Environment-backed paths such as
`LOCAL_SOURCE_PATH=./docs` are resolved from the process working directory.

Callbacks and custom parser objects are Python values and cannot be represented
safely in YAML. Configure `custom_parsers`, `process_attachment_callback`, and
`process_file_callback` by constructing the provider config in code or by using
an explicit code override.

## Operational tuning

HTTP connector settings are static configuration, not automatic tuning:

| Setting | Constraint |
|---|---|
| `requests_per_minute` | Integer from `1` through `6000`. |
| `request_timeout_seconds` | Number greater than `0`. |
| `max_retries` | Integer greater than or equal to `0`. |
| `backoff_factor` | Number greater than or equal to `0`. |
| `page_size` | Provider-specific bounded integer. |

Construct a new connector after changing configuration. HTTP clients calculate
some state, including the minimum request interval, during construction; config
objects are not intended for live mutation.
