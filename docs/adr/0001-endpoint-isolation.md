# ADR 0001. Endpoint isolation and privilege

Status: Proposed

## Context

The endpoint SDK has no existing privileged broker protocol or platform isolation
configuration. An observe-only action still has the OS authority of its worker.

## Decision

Allow authorized observations through the configured worker. Deny mutating and
privileged registrations before any step starts. Use the in-process worker with
trusted code for local testing. Bind deployment-specific broker and isolation
behavior through reviewed adapters before enabling additional effects.

## Consequences

The journal quarantines interrupted runs and supports operator investigation.
A future broker adapter requires durable mutation intent, precise operation
authorization, revocation checks and reconciliation before automatic recovery.
