# Chat client data flow

## Synchronous

```text
caller
  → HarborChatClient
  → request preparation and validation policy
  → routing/retry/cache execution policy
  → LiteLLM backend
  → normalized HarborChatResponse or HarborChatStreamChunk
```

## Asynchronous

```text
caller
  → AsyncHarborChatClient
  → the same request preparation and validation policy
  → asynchronous routing/retry/cache execution policy
  → asynchronous LiteLLM backend
  → normalized HarborChatResponse or HarborChatStreamChunk
```

## Construction

```text
HarborChatClientConfig + ChatClientDependencies
  → ChatClientFactory
  → internal runtime composition
  → sync or async core protocol implementation
```

## Failure modes

- mutually exclusive backend and invocation injection fails during construction
- invalid provider/configuration fails before provider invocation
- closed clients reject new work
- provider failures are normalized through existing error policies
- partial streams emit the existing terminal error behavior without retrying
  after committed output
- owned resources close once; borrowed resources remain open
