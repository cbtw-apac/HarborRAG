# harborrag-mcp-server

Owns the FastMCP transport, pre-execution policy, and durable audit boundary.

## Folder ownership

```text
tools/base.py + tools/health.py
server/base.py + server/server.py
```

## Team deliverables

- The shipped transport exposes only `harborrag_health_check`.
- Every attempt and outcome is durably audited with a principal identifier and
  arguments digest; raw arguments and tokens are never recorded.
- Declared input schemas plus argument, result-count, and serialized-output
  budgets are enforced.
- Ingestion-capability tools fail closed until explicitly enabled.
- Transport creation fails closed without authentication unless the caller
  explicitly selects local unauthenticated stdio.
- Service-level tools must never expose raw database/provider access or place
  retrieved document text in descriptions.

Run the standard stdio transport:

```bash
python -c \
  "from harborrag_mcp_server import create_mcp_server; create_mcp_server(allow_unauthenticated_local=True).run()"
```

Network transports must be constructed with a FastMCP authentication provider.
The explicit local override is for stdio only and opens no listener.


## Package tests

Tests for this package live in:

```text
packages/harborrag-mcp-server/tests/
```

Run from the repository root:

```bash
pytest packages/harborrag-mcp-server/tests
```

Keep new tests in this folder when adding or changing behavior owned by this package.
