# Chat backend contract suite

Every chat transport backend must be exercised through `BackendContractHarness` and `exercise_backend_contract`.

The shared contract verifies:

- Stable backend identity.
- Synchronous completion.
- Asynchronous completion.
- Synchronous stream opening.
- Asynchronous stream opening.
- Backend-specific parameter transformation.
- Stream cleanup compatibility.
- Idempotent lifecycle behavior in backend-specific tests.

New backends should add one parametrized harness without duplicating the client
execution tests. Provider calls remain injected here; real transport
conformance belongs in the [model smoke suite](../../smoke/README.md).
