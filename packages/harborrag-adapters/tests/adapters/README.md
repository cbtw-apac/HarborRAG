# Top-level adapter tests

These tests check the base contracts every adapter family shares, and that a connector and a
parser work together. `unit/test_adapter_contracts.py` checks that:

- the base connector and parser methods raise `NotImplementedError` until a subclass implements
  them;
- the repository contracts are abstract;
- a connector's documents parse through a parser end to end.

Run them from the repository root:

```bash
uv run pytest packages/harborrag-adapters/tests/adapters
```

Provider-specific behaviour belongs in the connector, model, parser or repository module, not in
this suite. The [test index](../README.md) lists those modules.
