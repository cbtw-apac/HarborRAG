# Live model smoke checks

The scripts in this directory make real chat, embedding, and reranking calls
through the public Harbor clients. They are standalone programs—not pytest
tests—and never mock LiteLLM or a provider transport.

Setup, credentials, backend selection, safety rules, exit codes, and acceptance
criteria are documented in the [real smoke-test runbook](../README.md).

```bash
python packages/harborrag-adapters/tests/smoke/models/chat.py
python packages/harborrag-adapters/tests/smoke/models/embed.py
python packages/harborrag-adapters/tests/smoke/models/rerank.py
python packages/harborrag-adapters/tests/smoke/models/run_all.py
```

If this is not the main environment, exit code `2` means the target is pending
because its real provider configuration is unavailable.
