# Top-level adapter tests

This module covers `AdapterBuilder`, `AdapterRegistry`, package-level provider
slots, and the minimal cross-family composition exposed by the adapter package.

```text
adapters/
  unit/       deterministic builder, registry, base-contract, and test-double checks
  README.md
```

Run the module from the repository root:

```bash
python -m pytest packages/harborrag-adapters/tests/adapters
```

Provider-specific behavior belongs to the connector, model, parser, or
repository module instead of this top-level suite.
