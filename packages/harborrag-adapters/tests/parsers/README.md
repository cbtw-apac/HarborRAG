# Parser tests

This module owns built-in document formats, parser routing, and PDF engines.

| Type | Scope |
| --- | --- |
| `unit/` | Format extraction, routing, metadata, shared utilities, and PDF backend behavior |
| `failure/` | Corrupt, unsupported, and malformed input normalization |
| `security/` | Archive bombs, safe paths, input coercion, and hardened XML parsing |
| `performance/` | Input limits, concurrent parsing, bulk scale, and expensive backend reuse |
| `smoke/` | Real local documents and explicitly selected PDF engines/profiles |

Run deterministic parser coverage with:

```bash
python -m pytest packages/harborrag-adapters/tests/parsers
```

Real documents and optional PDF/OCR engines may need native libraries, model
downloads, network access, and substantial CPU/GPU resources. Follow the
complete [parser smoke setup](smoke/README.md) before running them.
