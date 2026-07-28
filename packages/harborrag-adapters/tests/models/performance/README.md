# Load tests

These bounded local tests verify concurrency control and single-flight behavior without making paid provider calls. Run them independently with:

```bash
python -m pytest packages/harborrag-adapters/tests/models/performance -m load
```

They are correctness-oriented micro-load tests, not capacity benchmarks. Production capacity tests should use the deployment manifests and workload profiles documented in `docs/CHAOS_LOAD.md`.
