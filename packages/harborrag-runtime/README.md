# harborrag-runtime

Owns local jobs, worker supervision, scheduling, runtime services, and future durable workflow integration.

## File configuration

Runtime owns the versioned connector YAML loader because file parsing, secret
resolution, and application composition sit outside provider adapters:

```python
from harborrag_runtime.config import load_connector_catalog

catalog = load_connector_catalog("config/connectors.yaml")
connectors = catalog.build_enabled()
```

Parser profiles use the same package:

```python
from harborrag_runtime.config import load_parser_catalog

catalog = load_parser_catalog("config/parsers.yaml")
parser_registry = catalog.build_harbor_parser()
```

See `docs/users/configuration/connector-config.md` for the schema, precedence,
and secret-handling rules.

Configuration code is split by responsibility:

```text
config/
  connectors/
    loader.py    # connector YAML file and schema-version handling
    schema.py    # connector definition validation and YAML boundaries
    models.py    # connector definitions, catalog, and construction
    providers.py # connector aliases and config metadata
  parsers/
    loader.py    # parser YAML file and schema-version handling
    schema.py    # parser and PDF-engine definition validation
    models.py    # parser catalog, definitions, and registry replacement
    providers.py # parser and PDF-backend constructor metadata
  utils.py       # reusable YAML, boolean, version, and mapping helpers
  errors.py      # shared configuration exception hierarchy
```

## Folder ownership

```text
jobs/base.py + jobs/mock.py
supervision/base.py + supervision/mock.py
scheduling/base.py + scheduling/mock.py
services/base.py + services/mock.py
```

## Team deliverables

- Implement durable job store.
- Implement bounded worker supervisor.
- Implement store-backed schedules.
- Add optional Temporal workflows without making Temporal a core dependency.


## Package tests

Tests for this package live in:

```text
packages/harborrag-runtime/tests/
```

Run from the repository root:

```bash
pytest packages/harborrag-runtime/tests
```

Keep new tests in this folder when adding or changing behavior owned by this package.
