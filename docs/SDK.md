# Workflow and SDK contracts

A workflow uses `apiVersion: remote.intent/v1` and `kind: Workflow`. Its
`spec.target.device_id` selects an inventory target. Each `spec.steps` entry
requires an ID, a registered `uses` key and a `with` mapping. The workflow may
also specify `request_reason`, a timeout and named output references. The
[workflow schema](generated/workflow.schema.json) describes the wire fields.

The parser accepts one bounded YAML mapping. Plain scalars use JSON-style
booleans, null and finite decimal numbers. Quoted scalars remain strings.
The parser normalizes Unicode to NFC and rejects aliases, anchors, tags, merge
keys and duplicate mapping keys.

The registry requires strict Pydantic object models with forbidden extra
fields. Model validators and serializers run inside the trusted code boundary.
Keep validators free of side effects because the compiler checks literal inputs
before execution. Supply deployment-verified implementation hashes when
registering callables. `publish_catalog` signs the registry's target snapshot.
A catalog consumer can authenticate it with `verify_catalog` and check exact
local parity with `verify_installed_actions`.

## References and dependencies

- Use `{ref: steps.collect.outputs.field}` to pass a declared output field.
  A reference requires matching producer and consumer schema constraints.
- Use `fallback` on the reference mapping when the producer may skip or omit
  the field. The fallback must satisfy the schema.
- `depends_on` lists steps that must succeed. `after` maps upstream step IDs
  to accepted terminal states and is available to observation actions.
- `when` supports `equals`, `in` and `exists` against published outputs.
  Use `all` and `any` to compose comparisons.
- Missing required referenced inputs fail before the consumer is invoked.
  A missing selected output without a fallback is omitted from the result.

## Admission and execution

The controller uses a host-authenticated principal. It evaluates policy and
signs a plan using Ed25519. The endpoint authenticates the canonical bytes,
matches the target, checks the validity window, recompiles against its
installed actions and re-evaluates policy. A configured approval provider
verifies a receipt bound to the exact plan and resource scope. Endpoint
admission denies privileged and mutating registrations pending the broker
and worker isolation integration.

Wire encoding is UTF-8 JSON with sorted object keys, compact separators,
finite numeric values and no duplicate keys. A signature covers the canonical
object containing the signing key ID and all plan claims. The SQLite replay
ledger claims an execution ID and nonce in one transaction. An identical
delivery attaches to that record. Conflicting reuse is rejected. The fake
controller tracks submission keys in memory for its process lifetime.

Steps run when their dependencies settle, under the configured endpoint
worker and an eight-slot cap. Cancellation signals cooperative workers.
Status and results are principal-scoped. The endpoint returns projected
inline results only when the deployer supplies a projection function.

The in-memory artifact scope stores bounded data for one execution. A handle
is unguessable and checked against its stored digest and scope. The storage
lifetime ends with the execution. The SDK has no artifact export adapter.

Deployments can explicitly enable the `data_export` effect with
`EndpointRuntime(allow_data_export=True)`. Admission and step policy still
apply. The publisher's registered callable owns the destination integration,
and a trusted worker can supply a short-lived grant via `ActionContext.services`.
An uncertain export is journaled for reconciliation. See the
[Docker fixture](DOCKER_E2E_EXTERNAL_EXPORT.md) for the separate delivery and
read-access paths.
