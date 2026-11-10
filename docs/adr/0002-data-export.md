# ADR 0002 · Registered data export

SafeRemote distinguishes observation, external data export, and mutation.
Endpoint admission denies data export by default. A deployment must opt in
and supply policy that authorizes the exact target, principal, workflow and
resolved step arguments. General mutation and privileged actions remain denied.

The fixture reuses ActionContext.services through a trusted worker adapter.
The fixture transport provisions an exact-blob SAS separately from the signed
plan over mutually authenticated TLS. The worker checks execution, target,
plan, step, destination and expiry before injecting the lease into the export
callable. The example pack owns the Azure client and destination semantics.
The fixture adds an authenticated MCP read-grant tool without changing the
core MCP tool set.

The example uses conditional creation with storage retries disabled. An
uncertain export is quarantined with RECONCILIATION_REQUIRED. Recovery
requires trusted inspection of the preallocated object and journal before an
operator considers a retry. Automatic reconciliation and durable controller
submission state are outside this fixture.
