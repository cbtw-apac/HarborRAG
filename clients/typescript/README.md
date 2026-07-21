# @harborrag/api-client

TypeScript client for the HarborRAG Control Plane API. **The backend owns the
contract**: wire types are generated from `openapi.json` (exported by
`make openapi` from `create_fastapi_app()`), never hand-written.

## Regenerate

```bash
make openapi                 # repo root: writes openapi.json
cd clients/typescript
npm install
npm run generate             # writes src/schema.d.ts
npm run build
```

CI (`.github/workflows/contract.yml`) does this on every PR and gates merges
on `oasdiff breaking` against the target branch (`contract-break-approved`
label overrides, requires lead review).

## Usage (WebUI)

```ts
import { createHarborClient } from "@harborrag/api-client";

const api = createHarborClient({
  baseUrl: import.meta.env.VITE_API_BASE_URL,
  getToken: () => localStorage.getItem("harbor_token"),
});

const health = await api.get<{ status: string; version: string }>("/health");
```

All non-2xx responses throw `HarborApiRequestError` carrying the standard
envelope `{error: {code, message, details, trace_id}}`.
