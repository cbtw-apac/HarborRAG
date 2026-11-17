# Parser tests

These tests cover the built-in document formats, parser routing and the PDF engines.

| Folder | What it covers |
| --- | --- |
| `unit/` | Format extraction, routing, metadata, shared utilities and PDF backend behaviour |
| `failure/` | Corrupt, unsupported and malformed input, normalized into parser errors |
| `security/` | Archive bombs, PowerPoint amplification, safe input paths and hardened XML parsing |
| `performance/` | Input size limits, concurrent parsing, bulk scale and reuse of expensive backends; all marked `slow` |
| `smoke/` | A standalone script that parses one real local document |

Run the collected tests from the repository root:

```bash
uv run pytest packages/harborrag-adapters/tests/parsers
```

Real documents and the optional PDF and OCR engines can need native libraries, model downloads,
network access and a lot of CPU or GPU. Set them up with [Parser smoke checks](smoke/README.md)
before you run the smoke script.
