# Chaos tests

These deterministic tests inject provider, Redis-lock, circuit, and cancellation failures without external infrastructure. They run in the normal quality gate and are marked `chaos` for targeted execution:

```bash
python -m pytest packages/harborrag-adapters/tests/chaos -m chaos
```
