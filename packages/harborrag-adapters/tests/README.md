# Adapter Test Strategy

The adapters suite is organized by execution scope first, then by specific
behavior inside each scope. Keep root-level files limited to shared fixtures and
helpers; avoid catch-all files such as `test_performance.py`,
`test_security.py`, or `test_coverage_boost.py`.

```text
tests/
  smoke/        standalone real connector smoke scripts driven by repo-root .env
  unit/         hermetic parser, connector, base, registry, and test-double tests
  failure/      hermetic error normalization and recovery tests
  e2e/          local/fake-client public workflow tests
  integration/  cross-package composition and contract tests
  performance/ deterministic scale and resource-safety checks
  security/     hardening and hostile-input checks
```

Strategy markers:

```text
blackbox  public API behavior only
graybox   public behavior with observable internal signals, logs, routes, or fake clients
whitebox  internal architecture, private helpers, route tables, and contract internals
```

Provider tests that use fake clients belong in `unit/` with `graybox`. Smoke
scripts use `HarborConnector` against real providers. They do not use pytest.
They load repo-root `.env` values, print the discovered records and loaded
`RawDocument`, and return a non-zero exit code when a provider is not configured
or fails to load data.

Run one provider smoke test:

```bash
python packages/harborrag-adapters/tests/smoke/jira.py
```

Run every configured provider smoke test:

```bash
python packages/harborrag-adapters/tests/smoke/run_all.py
```

A script exits `2` and prints which variables are missing if its provider
isn't configured, so it's safe to leave providers you don't use unset — only
the ones you fill in get exercised (`run_all.py` skips exit-`2` providers
without failing the run). Copy `.env.connector.example` to repo-root `.env` and fill in
the providers you want to test; `_bootstrap.load_env()` reads it directly
without requiring a real shell export.

Confluence and JIRA additionally load their first discovered record twice —
once with `include_attachments=False`, once with `True` — and print an
`attachments:` block for each attachment (title, status, size, extracted-text
char count), so you can see the with/without-attachments difference in one
run. Every printed value (document content, metadata, raw payload, attachment
text) is truncated to 200 chars so a large PDF/DOCX doesn't flood the
terminal; the truncation marker also reports the untruncated length.

### Per-provider configuration

| Provider | Script | Required `.env` vars | Optional `.env` vars |
| --- | --- | --- | --- |
| Confluence | `smoke/confluence.py` | `CONFLUENCE_BASE_URL`, `CONFLUENCE_SPACE_KEY`, `CONFLUENCE_TOKEN` | `CONFLUENCE_EMAIL` (required only for Cloud; omit for Data Center) |
| JIRA | `smoke/jira.py` | `JIRA_BASE_URL`, `JIRA_TOKEN` (or `JIRA_API_TOKEN`) | `JIRA_PROJECT_KEY` (searches all visible projects if unset), `JIRA_EMAIL` (Cloud only) |
| GitHub | `smoke/github.py` | `GITHUB_TOKEN`, and either (`GITHUB_OWNER` + `GITHUB_REPO`) or `GITHUB_REPOSITORY_URL` | `GITHUB_REF` (branch/tag/sha; defaults to the repo's default branch) |
| SharePoint | `smoke/sharepoint.py` | Either `SHAREPOINT_SITE_URL` or `SHAREPOINT_SITE_ID`, **and** either `MICROSOFT_GRAPH_TOKEN` or all three of (`MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`) | `SHAREPOINT_DRIVE_NAME` (defaults to the site's default document library) |
| Local files | `smoke/local.py` | `LOCAL_SOURCE_PATH` (a file or directory that exists on disk; relative paths resolve from the repo root) | — |

Notes:
- `GITHUB_REPOSITORY_URL` takes a full URL like `https://github.com/abc/harbor-rag.git` or `git@github.com:abc/harbor-rag.git`; don't set `GITHUB_REPO` to a URL when using the `GITHUB_OWNER`/`GITHUB_REPO` pair — it must be the bare repo name.
- SharePoint's `MICROSOFT_GRAPH_TOKEN` is a short-lived (~1 hour) pre-issued Graph token; for anything longer-running, use the `MICROSOFT_TENANT_ID`/`MICROSOFT_CLIENT_ID`/`MICROSOFT_CLIENT_SECRET` client-credentials flow instead, which the connector refreshes itself.
- JIRA reads `JIRA_TOKEN` first and falls back to `JIRA_API_TOKEN` if unset — set either one.

When a file grows to cover multiple behaviors, split it by the thing under test:
for example HTTP utility tests, attachment processing tests, PDF memoization
tests, same-origin URL tests, and secret redaction tests should each live in
their own file.
