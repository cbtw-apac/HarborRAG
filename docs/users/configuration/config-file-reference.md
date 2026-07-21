# Engine Configuration

Connector, parser, and model settings have file loaders. The engine itself currently uses two small frozen dataclasses constructed in Python.

## `EngineConfig`

```python
from harborrag_engine.config import EngineConfig

config = EngineConfig(tenant="default", environment="local")
```

| Field | Default | Meaning |
| --- | --- | --- |
| `tenant` | `default` | Tenant label included in engine diagnostics |
| `environment` | `local` | Free-form environment label included in diagnostics |

## `EnginePolicy`

```python
from harborrag_engine.policy import EnginePolicy

policy = EnginePolicy(max_concurrency=4, retrieval_top_k=10)
```

| Field | Default | Meaning |
| --- | --- | --- |
| `max_concurrency` | `4` | Engine concurrency policy; must be at least one |
| `retrieval_top_k` | `10` | Default retrieval result count |

`EngineBuilder(config=config, policy=policy).diagnostics()` exposes the environment, tenant, and concurrency values. The current `CompositionRoot.local()` uses defaults.

There is no unified application configuration that automatically combines engine policy, connector/parser catalogs, models, repositories, API, and runtime services. Production composition remains application code.
