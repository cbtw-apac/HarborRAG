# Runtime chat

Contributor notes for `harborrag_runtime.chat`, which builds the chat model clients and
owns their lifecycle. Transports such as the HTTP API and the CLI reach chat through
`ChatFacade` (`HarborRAG.chat`); they never build model adapters or read prompt files.

```text
chat/
├── facade.py              # ChatFacade, the SDK surface: complete, stream, validate_model
├── service.py             # RuntimeChatService: client selection and async lifecycle
├── composition.py         # model catalog to AsyncHarborChatClient wiring
├── tenant_clients.py      # per-tenant chat clients, cached against the catalog fingerprint
├── tenant_config.py       # a tenant's stored chat catalog as a validated client configuration
└── tenant_models.py       # the two ports per-tenant resolution needs
```

## Which client answers a turn

The process-wide client is built from `config/models/models.yaml`. When the composition
calls `HarborRAG.configure_tenant_models`, a tenant that stored its own chat models gets
its own client instead, chosen per request from `request.metadata.tenant_id`. Any failure
to resolve a tenant's client falls back to the process-wide one. Without that call, every
tenant uses the process-wide client.

## Prompts

Prompt definitions and templates live in `harborrag_engine.chat.prompts`. They are
server-owned system messages: a selected template is prepended to the request messages,
and selecting no prompt leaves the messages unchanged. Templates hold behaviour
instructions only, never provider credentials, tenant data or model choices.
`ChatPrompt.DEFAULT` and `ChatPrompt.CONCISE` are the public prompts;
`ChatPrompt.QUERY_GATE` is the application's internal query gate.

To add a prompt:

1. Add a UTF-8 Markdown file under `harborrag_engine/chat/prompts/templates/`.
2. Add its stable public name to `ChatPrompt` and its filename to
   `PromptCatalog._FILENAMES`.
3. Add catalog tests (`packages/harborrag-engine/tests/test_chat_prompts.py`) and
   transport-contract tests.
