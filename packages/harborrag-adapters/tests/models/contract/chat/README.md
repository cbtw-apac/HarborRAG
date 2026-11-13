# Chat contract suites

One shared suite checks every chat client, and another checks every chat transport backend, so
each implementation meets the same contract. Provider calls are injected; no request leaves the
machine.

## Client contract

`ChatClientContractSuite` in `chat_client_contract.py` runs the same checks against
`HarborChatClient` and `AsyncHarborChatClient`: completion, streaming, structured output and
lifecycle, plus the protocol each client must implement.

## Backend contract

`exercise_backend_contract` in `chat_backend_contract.py` takes a `BackendContractHarness` and
checks that the backend:

- reports a stable backend type;
- completes synchronously and asynchronously;
- opens a stream synchronously and asynchronously;
- applies its own parameter transformation;
- closes streams it opened.

`test_chat_backends.py` runs it for the `direct_sdk`, `litellm_router` and `litellm_proxy`
backends, and checks the shared connection lifecycle separately. A new backend adds one test that
builds its harness and awaits `exercise_backend_contract`, instead of copying the client tests.

## Run the suites

The tests are marked `contract`. Run them from the repository root:

```bash
uv run pytest packages/harborrag-adapters/tests/models/contract -m contract
```

Conformance against a real provider transport belongs in the
[model smoke checks](../../smoke/README.md).
