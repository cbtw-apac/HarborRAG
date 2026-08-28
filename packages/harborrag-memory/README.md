# harborrag-memory

Scope-aware memory shared by chat and agent orchestration in
[HarborRAG](https://github.com/cbtw-apac/HarborRAG). This package owns the memory facade;
storage lives in `harborrag-adapters` and the contracts live in `harborrag-core`.

It is a required dependency of `harborrag-runtime`, so any HarborRAG install already has it.

```bash
pip install harborrag-memory
```

## Three tiers, one facade

| Tier | Holds | Lifetime |
| --- | --- | --- |
| Short-term | conversation turns | the session |
| Working | per-run scratch state | the run, bounded by a TTL |
| Long-term | durable memories in a repository | until deleted |

`MemoryManager` is the single facade chat and agent code imports. Each tier is optional -
pass only the ones you configured, and calling into an unconfigured tier raises
`MemoryConfigurationError`.

## Everything is scoped by `MemoryOwner`

Every public operation takes a `MemoryOwner`: the isolation key a memory is written under,
or the identity a query is issued as.

```python
from harborrag_memory import MemoryOwner

owner = MemoryOwner(
    tenant_id="tenant-1",
    project_id="handbook",
    principal_id="user-1",
    session_id="session-1",
    run_id="run-1",
)
```

How much of that owner is load-bearing depends on the memory's `MemoryScope`:

| Scope | Owner fields that must match |
| --- | --- |
| `GLOBAL` | none |
| `TENANT` | `tenant_id` |
| `PROJECT` | `tenant_id`, `project_id` |
| `USER` | `tenant_id`, `principal_id` |
| `SESSION` | `tenant_id`, `principal_id`, `session_id` |
| `RUN` | every field through `run_id` |

> **Security rule.** Build the owner from authenticated request context. Never pass
> user-supplied owner fields through - doing so lets a caller read another tenant's,
> project's, or user's memory. `snapshot()` enforces this by rejecting a query whose owner
> does not match the authenticated caller.

## Worked example

```python
import asyncio

from harborrag_memory import (
    InMemoryWorkingMemoryStore,
    MemoryManager,
    MemoryOwner,
    WorkingMemory,
)


async def main() -> None:
    owner = MemoryOwner(
        tenant_id="tenant-1",
        principal_id="user-1",
        session_id="session-1",
        run_id="run-1",
    )
    memory = MemoryManager(working=WorkingMemory(InMemoryWorkingMemoryStore()))

    await memory.update(owner, {"step": "retrieval", "candidates": 12})
    print(await memory.scratch(owner))          # {'step': 'retrieval', 'candidates': 12}

    snapshot = await memory.snapshot(owner)     # recent turns + working state + memories
    print(snapshot.working_state)


asyncio.run(main())
```

## Facade surface

| Tier | Methods |
| --- | --- |
| Short-term | `recent(owner, limit=...)`, `append(owner, turn)`, `clear(owner)` |
| Working | `scratch(owner)`, `update(owner, state, ttl_seconds=...)`, `clear_working(owner)` |
| Long-term | `save(caller, memory)`, `get(caller, memory_id)`, `search(caller, query)`, `delete(caller, memory_id)` |
| Combined | `snapshot(owner, query=..., recent_limit=...)` |

## Module ownership

- `tiers/short_term.py` - conversation history facade.
- `tiers/working.py` - per-run scratch state facade and local store.
- `tiers/long_term.py` - canonical repository facade for durable memory.
- `manager.py` - `MemoryManager`, the single facade chat and agent import.
- `schemas.py` - stable re-exports of the core-owned memory contracts.
- `config.py` - `MemoryManagerConfig`, including the default recent-turn limit.
- `errors.py` - `MemoryError`, `MemoryConfigurationError`, `MemoryScopeError`.

## Development

Tests for this package live in `packages/harborrag-memory/tests/`. Run them from the
repository root:

```bash
uv run pytest packages/harborrag-memory/tests
```

Licensed under the Apache License 2.0.
