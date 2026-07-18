# Configuration Reference

Connector instances and parser profiles can be loaded from YAML as documented in
[Connector Configuration](connector-config.md) and
[Parser Configuration](parser-config.md). Engine and complete pipeline
composition remain code-driven. This page documents the two small configuration
dataclasses that currently exist in `harborrag-engine`.

## `EngineConfig`

`packages/harborrag-engine/src/harborrag_engine/config.py`:

```python
@dataclass(frozen=True, slots=True)
class EngineConfig:
    tenant: str = "default"
    environment: str = "local"
```

| Field | Default | Meaning |
|---|---|---|
| `tenant` | `"default"` | The tenant identifier surfaced in diagnostics; see [Workspace / Multi-Tenancy](workspace-mode.md) for the related `Tenant` domain type. |
| `environment` | `"local"` | A free-form environment label (e.g. `local`, `dev`, `prod`) surfaced in diagnostics. |

## `EnginePolicy`

`packages/harborrag-engine/src/harborrag_engine/policy.py`:

```python
@dataclass(frozen=True, slots=True)
class EnginePolicy:
    max_concurrency: int = 4
    retrieval_top_k: int = 10
```

| Field | Default | Meaning |
|---|---|---|
| `max_concurrency` | `4` | Must be `>= 1`; validated in `__post_init__`, raises `ValueError` otherwise. |
| `retrieval_top_k` | `10` | Default number of retrieval results. |

## Seeing current values

```bash
python -m harborrag_app.cli.main doctor --json
```

```json
{"diagnostics": {"engine": {"environment": "local", "max_concurrency": 4, "tenant": "default"}, "runtime": {"provider": "mock_runtime", "ready": true}}, "ok": true}
```

`EngineBuilder.diagnostics()` (`packages/harborrag-engine/src/harborrag_engine/builder.py`) is what produces the `engine` block above.

## What's planned

A future configuration-driven `CompositionRoot` can consume the connector
catalog alongside engine, parser, model, repository, and feature-budget
configuration. Until that broader composition layer lands, changing
`EngineConfig` or `EnginePolicy` still means constructing them explicitly in
code.

## Related

- [Workspace / Multi-Tenancy](workspace-mode.md) — the `Tenant` and `RequestContext` primitives.
- [Connector Configuration](connector-config.md) — the validated connector YAML loader.
- [Extending HarborRAG](../../developers/extending/README.md) — provider and composition extension points.
