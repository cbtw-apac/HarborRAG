# Chaos tests

These tests inject failures into the model runtime and check that it recovers: the distributed
circuit opens on provider failures and closes again, and the Redis single-flight lock is released
when its leader crashes or is cancelled. An in-memory Redis stand-in replaces the real server, so
the tests need no external service and run in the default test run.

They are marked `chaos`. Run only them from the repository root:

```bash
uv run pytest packages/harborrag-adapters/tests/models/chaos -m chaos
```
