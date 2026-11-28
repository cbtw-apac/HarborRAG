# Connector tests

These tests cover the shared connector contracts and the Local, GitHub, Confluence, Jira and
SharePoint connectors.

| Folder | What it covers |
| --- | --- |
| `unit/` | Configuration, discovery and load mapping, HTTP clients, filters, attachments and utilities, with fakes |
| `failure/` | Retry, rate limits, authentication, fetch errors and normalized failures |
| `security/` | Same-origin URL checks, secret redaction and the redacted smoke-script output |
| `performance/` | Bounded response streaming and download caps |
| `smoke/` | Standalone scripts that discover and load from a real folder or provider, plus collected tests of their helpers |

Run the collected tests from the repository root:

```bash
uv run pytest packages/harborrag-adapters/tests/connectors
```

The smoke scripts call real providers, so they need credentials, network access, a scoped source
and the attachment parsers, and sometimes an OCR server. Set them up with
[Connector smoke checks](smoke/README.md) before you run one.
