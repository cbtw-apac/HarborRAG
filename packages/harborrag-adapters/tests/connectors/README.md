# Connector tests

This module owns shared connector contracts and the Local, GitHub, Confluence,
JIRA, and SharePoint implementations.

| Type | Scope |
| --- | --- |
| `unit/` | Configuration, discovery/load mapping, HTTP clients, filters, attachments, and utilities with fakes |
| `failure/` | Retry, rate-limit, authentication, fetch, and normalized failure behavior |
| `security/` | Same-origin URL enforcement, secret redaction, and safe smoke output |
| `performance/` | Bounded response streaming and download caps |
| `smoke/` | Real filesystem and provider discovery/load operations |

Run deterministic connector coverage with:

```bash
python -m pytest packages/harborrag-adapters/tests/connectors
```

Real providers require credentials, network access, source scoping, optional
attachment parsers, and sometimes PDF/OCR models. Follow the complete
[connector smoke setup](smoke/README.md) before executing those scripts.
