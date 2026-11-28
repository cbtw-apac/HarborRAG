# Load tests

These bounded local tests check that the model runtime stays correct under concurrent load: one
hundred concurrent identical requests coalesce into a single call, and admission control holds its
concurrency limit during a burst. They make no provider calls.

They are correctness checks, not capacity benchmarks. They are marked `performance` and `load`;
run only them from the repository root:

```bash
uv run pytest packages/harborrag-adapters/tests/models/performance -m load
```
