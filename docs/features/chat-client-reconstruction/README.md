# Chat client reconstruction

## Problem

The adapter chat client combines synchronous and asynchronous contracts,
dependency composition, backend construction, lifecycle ownership, structured
output, streaming, and batching in one public class. This makes the two
execution modes easy to change independently without a shared conformance test
and gives the constructor too many unrelated reasons to change.

## Scope

- provide distinct synchronous and asynchronous clients implementing the core
  protocols
- compose runtime dependencies once behind a typed dependency schema
- provide a narrow factory for turning validated configuration into either
  client
- run one contract suite against both clients
- document the public surface and its package boundary

## Non-goals

- adding a chat provider plugin registry
- changing LiteLLM routing, retry, fallback, caching, or security behavior
- wiring Langfuse or OpenTelemetry exporters
- redesigning embedding or reranking clients
- changing core chat request or response schemas

## Public contracts

`HarborChatClient` implements `HarborChatClientProtocol`.
`AsyncHarborChatClient` implements `AsyncHarborChatClientProtocol`.
`ChatClientFactory` creates either client from `HarborChatClientConfig` and an
optional `ChatClientDependencies`.

## Dependencies

The public clients depend on core request/response protocols. Adapter-owned
runtime composition depends on existing LiteLLM backends, routing policies,
cache, telemetry, budget, and lifecycle boundaries.

## Migration

Asynchronous call sites move from `HarborChatClient.achat` and
`HarborChatClient.astream` to `AsyncHarborChatClient`. Constructor boundary
arguments move into `ChatClientDependencies`.

## Removal plan

Remove asynchronous methods from the synchronous client, remove the duplicated
`from_config` constructor, and leave no aliases for the combined client API.
