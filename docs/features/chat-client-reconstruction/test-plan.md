# Chat client reconstruction test plan

## Test strategy

### Unit

- dependency validation and ownership
- factory creates the requested client type
- sync and async clients expose only their intended completion methods
- lifecycle remains idempotent

### Contract

One parametrized suite verifies both core chat protocols:

- normalized completion
- explicit request and model selection
- streaming completion events
- structured response validation
- closed-client behavior

The suite uses an in-memory invocation fake and performs no network I/O.

### Integration

Existing backend contract and model execution tests continue to cover direct,
router, and proxy backend behavior. No real provider call is added in this
phase.

## Failure paths

- invalid request
- provider error normalization
- close followed by invocation
- stream failure after output
- structured-output validation failure

## Acceptance

- sync and async clients pass the same contract suite
- the existing chat test suite passes after migration
- `lint-imports` passes
- no chat source file exceeds 350 lines
- the Phase 1 Ruff baseline does not regress; remaining repository-wide
  complexity and argument-count findings are reported for later cleanup
