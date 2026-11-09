# Graph Build Configuration

[`config/graph/build.yaml`](../../../config/graph/build.yaml) decides how the knowledge
graph is built: runtime limits, entity summarization, the extraction vocabularies and the
per-tenant policy. The runtime reads it from `HARBORRAG_GRAPH_BUILD_CONFIG_PATH`
(default `config/graph/build.yaml`). When that variable is unset and the file is missing,
the built-in defaults apply, and they make no LLM calls. An explicitly set path that does
not exist is an error.

Print the effective values, and check the file, with:

```bash
harborrag topology config validate
```

`harborrag topology config apply` writes the tenant and source policy to the control
database.

## File layout

```yaml
runtime:
  operation_timeout_seconds: 180.0
  llm_operation_cost_usd: "0.01"
  derived:
    enabled: true

summarization:
  enabled: false
  tenant_enabled: false
  max_description_tokens: 512
  budget:
    max_calls_per_document: 256

ontologies:
  example: ontologies/example.yaml

tenants:
  - tenant_id: DEFAULT
    mode: llm
    budget:
      max_concurrency: 2
      daily_token_cap: 200000000
      daily_cost_usd: "15.00"
    sources:
      - source_scope_id: harborrag-docs
        ontology: example
```

Quote money values so they keep their exact decimal value. Unknown and duplicate keys are
rejected at load time.

## Summarization

| Key | Meaning |
| --- | --- |
| `enabled` | Writes summary cards for parent nodes from their children's content |
| `tenant_enabled` | Lazy, on-demand tenant-level summaries. Opt-in |
| `model` | Logical chat model for summaries. Omit it to use the default model of `config/models/models.yaml` |
| `max_description_tokens` | Size of each generated summary |
| `budget.max_usd_per_run` | Stops the run once crossed. Needs pricing on the model deployment |
| `budget.max_calls_per_document` | Upper bound of summarization calls per document |

Facets, the filterable facts of an entity card, are declared per source; see
[Facets](../detailed-guides/mcp-server/setup-and-integration.md#facets-the-filterable-part-of-a-card).

## Extraction vocabularies

An ontology is the closed set of entity types and relation predicates the LLM may emit.
It is compiled into the structured-output JSON schema as literal enums, so the model
cannot return a type the file does not list.
[`config/graph/ontologies/example.yaml`](../../../config/graph/ontologies/example.yaml)
is a worked example. To use one, name it under `ontologies:` (paths are relative to
`build.yaml`) and select it on a source with `ontology:`, as in the layout above.

Changing an ontology changes the extraction profile fingerprint, so the affected
documents are extracted again instead of mixing two vocabularies.

TODO: move the per-tenant `tenants:` blocks out of `config/graph/build.yaml` into tenant settings.
