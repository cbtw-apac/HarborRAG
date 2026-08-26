# Runtime chat

This package owns provider-neutral chat orchestration. Transport packages call
the `ChatFacade`; they do not construct model adapters or read prompt files.

```text
chat/
├── facade.py              # stable HarborRAG SDK surface
├── service.py             # prompt application and async client lifecycle
├── composition.py         # model-catalog to AsyncHarborChatClient wiring
└── prompts/
    ├── catalog.py         # typed prompt names and safe resource lookup
    └── templates/
        ├── default.md
        └── concise.md
```

Prompt templates are server-owned system messages. A selected template is
prepended before request messages; selecting no prompt leaves messages
unchanged. Templates never contain provider credentials or tenant data.

To add a prompt:

1. add a UTF-8 Markdown file under `prompts/templates/`;
2. add its stable public name and filename to `ChatPrompt` and
   `PromptCatalog._FILENAMES`;
3. add catalog and transport-contract tests.

Model/provider selection remains in `config/models.yaml`; prompt files should
contain behavior instructions only.
