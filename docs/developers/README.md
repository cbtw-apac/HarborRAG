# Developer Documentation

Build, test, and operate HarborRAG from a repository checkout.

| I want to… | Start here |
| --- | --- |
| Make my first contribution | [Set up a checkout](checkout-quick-start.md) and read the [contribution guide](../../CONTRIBUTING.md). |
| Understand the path from a document to an answer | [Data lifecycle](architecture/data-lifecycle.md) and the [package map](architecture/README.md#package-map). |
| Add a connector, model, parser, or endpoint | [Extending HarborRAG](extending/README.md). |
| Find the right test | [Testing](testing/README.md). |
| Operate the services | [Deployment](deployment/README.md), [runtime reliability](architecture/runtime-reliability.md), and [projection rebuild](architecture/projection-rebuild.md). |
| Prepare a release or public docs | [Release process](release-process.md) and [publication guidelines](publication-guidelines.md). |

## Development principles

- Keep provider-neutral data and errors in `harborrag-core`; keep provider SDKs in
  `harborrag-adapters`.
- Prefer narrow dependency injection over a global settings object.
- Preserve source identity, permissions, content type, timestamps and parser warnings across
  ingestion stages.
- Treat the embedding model and its dimension as part of a vector space's identity.
- Require explicit tenant context on repository operations.
- Keep default tests hermetic; put live checks behind opt-in smoke scripts.
- Redact secrets and bound logged content at every provider and observability boundary.
- Document implemented behaviour separately from scaffolded intent.

Before opening a pull request, run the
[quality gates](../../CONTRIBUTING.md#quality-gates).
