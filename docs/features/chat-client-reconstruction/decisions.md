# Chat client reconstruction decisions

## Contract / Policy / Implementation

- Contract: the sync and async protocols in `harborrag_core.models.protocols`
- Policy: existing validation, routing, retry, fallback, structured-output,
  cache, budget, and security modules
- Implementation: LiteLLM direct, router, and proxy backends plus chat
  execution and streaming

## Pattern decision

Factory is used because callers need a ready sync or async client from validated
configuration. Strategy remains the shape of routing and structured-output
behavior. Existing registries remain limited to real provider metadata and
backend variation points. Builder is not used because client construction is
not an ordered, stepwise process. No provider plugin registry is added because
LiteLLM is still the single provider execution path.

## Public surface

The package root exports clients, their factory/dependency schema, primary
configuration, and result/policy types required by public method signatures.
Backend implementations and runtime composition remain internal.

## Observability

Existing provider-neutral telemetry injection remains supported. Exporter
integration is deferred until after the client contract suite passes.
