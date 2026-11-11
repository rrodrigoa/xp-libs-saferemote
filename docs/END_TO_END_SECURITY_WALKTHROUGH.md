# Follow one SafeRemote request from the agent to the machine

SafeRemote lets an application request execution of registered Python
callables on an endpoint. The control plane decides whether a request may
run. The endpoint checks that decision against its local configuration
before invoking installed code. The same interfaces apply to any supported
action contract.

The running example asks an agent to collect up to 100 records from a
synthetic log source on one endpoint and export them to an approved
destination. The Docker fixture reads a fixed file with three records.
An action publisher chooses the data source, callable and input model.
The deployment installs the code and grants the worker the necessary OS
permissions. The example blob upload demonstrates a separately authorized
export. Other deployments can handle data retrieval through their own
integration or use the library for execution alone.

Read steps 1–14 in order. They follow the same request throughout. Each
step explains what gets passed, who checks it, and what would go wrong if
that check were missing. Values in angle brackets are illustrative
placeholders for values produced at runtime.

## The whole request on one page

```text
Operator asks for logs
    |
    v
Agent host adds the HTTP credential
    |  HTTPS: bearer header + MCP tool arguments
    v
FastMCP authenticates caller -> principal "owner"
    |  Discover action contracts -> receive catalog epoch
    |  Validate and preview YAML -> no action calls
    |  Submit YAML + epoch + idempotency key
    v
Controller recompiles -> policy check -> approval if required -> signs plan
    |  mTLS: signed plan + separately scoped export grant
    v
Endpoint checks signature, target, time and grant scope
    |  Recompiles against installed actions -> checks local policy
    |  Atomically records execution ID and nonce before invoking anything
    v
Worker collects local logs -> in-memory artifact handle
    |
    v
Worker checks resolved arguments -> uploads under a one-blob write grant
    |  HTTPS: log bytes directly to storage
    v
Endpoint records outcome and export receipt
    |  MCP: status, object reference, SHA-256, byte count, ETag
    v
Authorized host requests a separate read grant
    |  HTTPS: host downloads log bytes directly from storage
    v
Host checks downloaded bytes against the receipt
```

The [logical diagram](diagrams/logical.svg) labels the interfaces L1 through
L8. The [physical diagram](diagrams/physical.svg) shows which Docker
containers actually open network connections. Start with the sequence above
when explaining the design. Use the logical diagram to discuss who owns each
check and the physical diagram to discuss certificates, network access and
where evidence bytes are visible.

## 1. Before the request, the publisher installs the allowed operations

The deployment loads two registrations into the controller's and endpoint's
registries. Each registration has a Python callable, a versioned action key,
strict Pydantic input and output models, an effect classification, and an
implementation digest supplied by the deployment. The controller uses the
published metadata to compile. The endpoint uses its own registry to find
the callable to invoke.

| Installed operation | Inputs | Output and effect |
| --- | --- | --- |
| `test.evidence.collect@1` | Fixed fixture ID and integer record limit | An artifact handle and metadata. Effect is `observe`. |
| `test.evidence.upload_blob@1` | An artifact reference and the allowed destination alias | A storage receipt. Effect is `data_export`. |

The `@1` in the action key selects major version 1. Discovery also reports
the full version, such as `1.0.0`, and the controller binds that resolved
contract into the plan. Even within the same major version, a changed
implementation or schema can change what the endpoint would execute.

**Why this comes first.** A natural-language request such as “get the logs”
must resolve to a capability somebody already reviewed and installed. If the
agent could supply a shell command, file path, Python module, or executable
download URL, it could try to expand that capability into reading unrelated
files or executing unrelated code. In this example, the code chooses a fixed
file, and the caller chooses only an allowed fixture ID and a bounded record
count. A registered collector could also use a log API under the same
constraints.
A registered callable itself is trusted publisher code. Its input schema
restricts requests made to it, and its worker's OS identity and isolation
restrict what the code can do on the machine.

See [the action implementations](../examples/action_packs/evidence_export/actions.py)
and [the registry contract](../src/remote_intent/actions.py).

## 2. The agent host authenticates to FastMCP (L1 → L2)

There are two participants on the client side. The model proposes a tool
name and its arguments. The host owns the HTTPS connection and attaches a
credential provisioned for that caller. A conceptual discovery request looks
like this. The JSON shows the MCP method inside the transport.

```http
POST /mcp HTTP/1.1
Host: control-plane:8443
Authorization: Bearer <host-held-secret>
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
  "name":"list_actions","arguments":{"target_id":"e2e-endpoint-01"}
}}
```

Two checks happen at different layers.

1. During TLS setup, the client checks the control-plane server certificate
   against its trusted CA and the requested hostname. TLS protects the header
   and tool message while they travel over that connection.
2. For a well-formed workflow tool call, the FastMCP adapter invokes the
   deployment's async `authenticate(ctx)` callback. In Docker the callback
   reads the HTTP `Authorization` header, rejects oversized values, and uses
   `hmac.compare_digest` to compare it with provisioned bearer values. Bootstrap
   generates separate random tokens for `owner`, `outsider`, and `approver`.
   A match returns the corresponding principal string. Missing or invalid
   credentials produce `MCP_SESSION_UNAUTHENTICATED`.

The **credential** is the secret the caller presents. The **principal** is
the identity the server derives after verification, for example `owner`.
The controller receives the principal and tool arguments. The bearer secret
stays at ingress. An argument such as `principal: owner` has no place in the
tool contract, and an unexpected argument is rejected by the MCP middleware.
The TLS private key also stays with its owner. The certificate is presented
to the peer, and the TLS handshake proves possession of the private key.

**Why both checks exist.** A valid server certificate lets the host know
where it is sending its secret. A valid bearer token lets the server know
which caller the request represents. If the service accepted a caller-supplied
identity string, a client could impersonate another principal. If transport
confidentiality were missing, someone who obtained a bearer token could
present it until it was invalidated or expired.

**Where authentication plugs in.** The library uses FastMCP, and
`create_server(controller, authenticate)` accepts an authentication
callback supplied by the host. The callback can read the header with
`get_http_headers(include={"authorization"})` and pass the credential
to the configured verifier. For a JWT, the verifier checks its signature,
issuer, intended audience, validity window and tenant restrictions where
applicable before mapping it to a stable principal. If a gateway supplies
an identity header,
authenticate that gateway and strip client-supplied copies of the header.
Configure host-level protection for the MCP connection, tool discovery and
resources where needed. The workflow tool callback is one layer. The
installed skill resource at `rir://skills/remote-intent/v1` is registered
separately.

See [FastMCP setup](../src/remote_intent/mcp.py) and the [fixture authenticator](../tests/e2e/control_plane.py).

## 3. The agent discovers what it can ask for and saves the catalog epoch (L3)

The authenticated host calls `list_actions(target_id)`. The controller
checks target visibility and filters the list to action keys visible to that
principal. The response carries an epoch and summary metadata. A shortened
response looks like this.

```json
{
  "status": "ok",
  "data": {
    "catalog_epoch": "<64 lowercase hexadecimal characters>",
    "actions": [
      {"uses": "test.evidence.collect@1", "version": "1.0.0",
       "description": "Collect bounded synthetic diagnostics", "effect": "observe"},
      {"uses": "test.evidence.upload_blob@1", "version": "1.0.0",
       "description": "Disclose synthetic evidence to the allowlisted investigation destination",
       "effect": "data_export"}
    ]
  }
}
```

The agent then calls `describe_action(target_id, name)` for each selected
key. These responses carry the input and output JSON Schemas, full version,
effect, implementation and schema digests, examples where provided, and the
current epoch. In the running example the collector's schema requires
`fixture_id` and `max_records`. The export schema requires `source` and
`destination`, and the destination is constrained to
`investigation-evidence`. The collector's input model permits up to 1,000
records. Policy for this export workflow caps the request at 100. Schema
validation and permission are separate checks.

**What the epoch actually is.** It is a string containing the SHA-256 digest
of the platform's complete action catalog serialized as canonical JSON. The
catalog is the list of published action specifications from the registry.
Canonical JSON uses a consistent UTF-8 encoding, sorted object keys, compact
separators and finite numbers. The digest includes the complete catalog
before visibility filtering, so two callers can receive the same epoch while
seeing different actions. The epoch carries no credential and grants no
permission. It can change when any catalog entry changes, including an
unrelated one. That is a deliberate cost of snapshot-wide consistency.

**Why the agent returns it.** The agent may discover a schema, spend time
composing YAML, and submit after a deployment changes the catalog. For
example, the same `@1` key might now resolve to a different implementation
or contract. With the original epoch supplied, the controller returns
`CATALOG_STALE` when it no longer matches. The host can rediscover the
actions, review the new contracts, and rebuild the request. Without an epoch
comparison, a YAML request that still validates could be compiled against a
new snapshot that the agent never inspected. The epoch also lets the host
notice that list and describe calls straddled a rollout. In this SDK version
the MCP argument is optional. An integration that requires discovery
consistency should always send it. Even if the caller omits it, the
controller compiles against its current catalog and the endpoint checks the
compiled plan against its local catalog.

**Separate fixture check.** The Docker runner also calls the fixture-only
`fixture_catalog` tool. It verifies a signed catalog with a preinstalled
public key and compares that snapshot's epoch with the ordinary listing.
The signature authenticates that published snapshot. The epoch is the
fingerprint used to compare snapshots. The nine core MCP tools do not require
the extra fixture tool.

See [catalog and visibility](../src/remote_intent/controller.py) and
[the runner's discovery sequence](../tests/e2e/agent_runner.py).

## 4. The agent builds the YAML request

The request says which target, which two registered actions, which input
values, and which results to return. This is the YAML form of the actual
Docker runner's workflow.

```yaml
apiVersion: remote.intent/v1
kind: Workflow
metadata:
  name: investigate-and-export-evidence
spec:
  target:
    device_id: e2e-endpoint-01
  steps:
    - id: collect
      uses: test.evidence.collect@1
      with:
        fixture_id: diagnostic-sample-01
        max_records: 100
    - id: export
      uses: test.evidence.upload_blob@1
      with:
        source:
          ref: steps.collect.outputs.artifact
        destination: investigation-evidence
  outputs:
    evidence:
      ref: steps.export.outputs.object_ref
    checksum:
      ref: steps.export.outputs.content_sha256
    size:
      ref: steps.export.outputs.size_bytes
    etag:
      ref: steps.export.outputs.etag
```

`device_id` names a deployment-managed inventory target. `destination`
names a publisher-allowed storage destination. `ref` names a field in a
declared step output. The source reference creates a dependency from
`export` to `collect`. No evidence bytes exist yet. The top-level
`outputs` only requests which fields should be selected for return. The
deployment still controls result projection.

**Why the destination is an alias.** Allowing an arbitrary URL would let
the caller propose an unreviewed destination for sensitive data. The alias
keeps routing and credential issuance in the deployment. Likewise, the
target ID is resolved through trusted configuration. The agent cannot turn
a target field into a delivery URL.

## 5. Validate the YAML and preview the decision (L4 → L5)

The host can make two optional MCP calls with the same argument object.
Use `validate_workflow` when the model is still assembling or repairing
the request. Use `preview_execution` when someone needs to see the effects
and current policy outcome before deciding whether to submit. If the host
already has the intended YAML and is ready to request execution, it can
call `submit_workflow` directly. Submit runs its own checks either way.
The HTTP credential is still attached at ingress on every call.

```json
{
  "workflow_yaml": "<the complete YAML document as a string>",
  "catalog_epoch": "<the digest returned by discovery>"
}
```

On each call, the controller confirms the principal can see the target and
selected actions. It checks the supplied epoch against the current catalog.
The parser accepts a bounded YAML document and rejects ambiguous or powerful
YAML features such as duplicate keys, aliases, anchors, tags and merge keys.
The compiler validates literal arguments using registered contracts. It
checks that references point to declared output fields with compatible
schemas, detects dependency cycles, and resolves the exact action
specifications.

The result is a Python Pydantic object called `CompiledPlan`. This is its
shape with the larger fields abbreviated.

```json
{
  "workflow": {"apiVersion": "remote.intent/v1", "kind": "Workflow",
               "metadata": "<validated metadata>", "spec": "<validated target, steps, limits and outputs>"},
  "catalog_epoch": "<catalog digest>",
  "actions": {"collect": "<complete resolved ActionSpec>",
              "export": "<complete resolved ActionSpec>"},
  "dependencies": {"collect": [], "export": ["collect"]},
  "order": ["collect", "export"]
}
```

The `actions` entries contain versioned identities, schemas and digests.
The actual Python callables are in the installed registries. The
**plan fingerprint** is a second SHA-256 hex string. Its input is the entire
compiled plan, including the workflow, action specifications, epoch,
dependencies and order. Two requests can use the same catalog epoch and
have different plan fingerprints, for example when the agent changes
`max_records`.

| Call | Checks run | Successful response | State saved by the SDK |
| --- | --- | --- | --- |
| `validate_workflow` | Authenticate, then compile and check visibility at L4 | `catalog_epoch`, `plan_digest`, `dependencies` | No submission or execution state. The temporary compiled plan is discarded. |
| `preview_execution` | Authenticate, compile again at L4, then evaluate policy at L5 | Same fields plus `authorization` and per-step `uses`, `version`, `effect`, and `privilege` | No submission or execution state. The compiled plan and policy decision are temporary. |
| `submit_workflow` | Authenticate, compile again at L4, evaluate policy again at L5, check retry state and any required approval, then sign and deliver | The delivery adapter's result. In Docker this is endpoint execution status. | A submission record in controller memory once the initial checks pass. The endpoint writes a separate SQLite record only if it admits delivery. |

### What L5 produces during preview

L4 answers, “What exactly does this YAML ask to do?” L5 answers, “May
this principal run that compiled request under current policy?” At
workflow scope, the policy evaluator receives the verified principal
and a copy of the compiled plan, with `step_id=None` and
`arguments=None`. The policy wrapper checks the decision and the
workflow timeout against the policy's limit.

For the Docker `owner` request, the internal result is equivalent to
`PolicyDecision(access=Access.ALLOW, revision="fixture-v1",
resources=("investigation-evidence",), timeout_seconds=120)`. This is a
Python object created by the policy adapter. The preview response
reports `authorization: "allow"` and the effects, including
`"data_export"` for `export`. It omits the decision's revision, resources
and timeout ceiling. If policy requires approval, preview reports
`authorization: "requires_approval"`. It performs no receipt lookup
and creates no execution ID. If policy denies, the tool returns a
structured error with `SECURITY_DENIED`.

The host can retain the preview for its own review or audit. The SDK
does not save it as an authorization record or accept a preview ID
later. A deployment's own policy backend, audit logging and receipt
provider have separate persistence. That is outside the preview
contract.

**Why these are separate.** A syntactically valid request may be forbidden.
For example, `outsider` can authenticate and discover the actions in Docker,
but policy denies that principal the export. Validation and preview invoke
zero registered actions. The compiler can run publisher-owned input
validators, which is why those validators must be side-effect-free.
Validation and preview allow the agent to repair a bad argument or present
an export for review without collecting or disclosing data. Preview reflects
the policy at that moment. Submit evaluates it again so that a policy change
between preview and submission can take effect.

See [the compiler](../src/remote_intent/compiler.py),
[the MCP calls](../src/remote_intent/mcp.py), and
[the Docker policy](../tests/e2e/common.py).

## 6. Submit the exact proposal and identify retries

The host calls `submit_workflow` with the YAML, the epoch, and an
`idempotency_key`. In this example the runner uses `evidence`. In a real
host, generate a stable key for each new user intent and retain it when
retrying that intent after a network failure.

```json
{
  "workflow_yaml": "<same YAML string>",
  "catalog_epoch": "<same catalog digest>",
  "idempotency_key": "evidence"
}
```

1. **Reauthenticate.** FastMCP derives the principal from the current
   request's credential. The tool accepts YAML, an optional catalog
   epoch and the idempotency key. It takes no plan digest, prior policy
   decision or approval receipt from the caller.
2. **Rebuild L4.** The controller calls the same compilation method
   used by validate and preview. It checks the current catalog and
   visibility, parses this submission's YAML, and reconstructs the
   resolved action specifications, dependencies, order and plan
   fingerprint. If discovery is stale or the request is invalid, the
   call stops here. A preview's in-memory plan is never looked up.
3. **Re-evaluate L5.** After checking the idempotency key's length, the
   controller calls policy with this principal and this rebuilt plan.
   Policy can now deny a request that preview allowed. If policy passes,
   its revision and sorted resource scope become claims in the plan
   that will be signed. The timeout allowance is enforced at the
   evaluation step. The resource scope records what policy permits.
   OS access and blob access each have their own permission checks.
4. **Check or create the submission record.** Under a lock, the controller
   uses `(principal, idempotency_key)` as the key to an in-memory dictionary.
   A new entry holds the fingerprint and an `ExecutionPlan` containing a
   deep copy of the compiled plan, the execution ID, nonce, principal,
   issue and expiry times, policy revision and sorted resources.
   Approval starts out empty. This is the first execution state saved
   by the SDK. It is written **before** the approval check in step 7,
   so even `APPROVAL_REQUIRED` can leave a record in controller memory
   while the endpoint has received nothing.
5. **On retry, compare before reusing.** Compilation and policy still
   run first. The controller compares the freshly built fingerprint to
   the stored one and rejects a mismatch with `PLAN_REPLAY_CONFLICT`.
   It rejects a changed revision or scope with `POLICY_STALE`. For a
   matching record it reuses the existing execution ID, nonce and
   validity window. If approval is required, it verifies the receipt
   again. When the checks pass, it signs and delivers using the stored
   execution identity.

**Why rebuild on submit.** Suppose a preview showed an allowed export,
then the caller's permissions changed. Submit asks policy again and
can deny it before delivery. If the action catalog changed instead,
the original epoch fails during compilation. Even with the same epoch,
changing `max_records` changes the compiled plan fingerprint. An earlier
preview cannot authorize those new arguments. The idempotency key
identifies a retry and carries no permission.

**Why the retry key exists.** Suppose the host submits, the endpoint
executes, and the network drops before the host gets its response. If
the host sends a fresh key, it may create a new execution. Reusing the
original key lets the controller recognize the same intent. The controller's
submission mapping is process-local. In a production system that must
deduplicate across replicas or restarts, design durable submission tracking
at the control plane. The endpoint journal protects a different boundary,
described in step 10.

## 7. If policy requires approval, verify the approval receipt

There are two different things called a *receipt* in this flow.

- An **approval receipt** exists before execution, and only when the
  applicable policy calls for it. It is evidence that the configured
  approval system authorized this exact execution request.
- An **export receipt** comes after a successful upload. It describes
  which object was written and what bytes the action reported writing.
  It is used to verify and retrieve the result in steps 13 and 14.

Here is how the optional approval branch works in the SDK. If the
decision is `requires_approval`, the controller computes an approval
binding after allocating the execution ID and expiry. The binding is a
SHA-256 digest over canonical JSON containing these fields.

```json
{
  "principal": "approver",
  "plan": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "revision": "fixture-v1",
  "resources": ["investigation-evidence"],
  "execution": "0123456789abcdef0123456789abcdef",
  "expires": 1760000900
}
```

These are illustrative values. The SDK sorts the resource list, encodes
the object as compact UTF-8 JSON with sorted keys, and hashes those bytes.
For the object above, the binding is the following 64-character string.

```text
b5e6bb0d181b20a8b3ade1dc28be83dde8c9cf8952e9e1e271ad4566002d643c
```

The binding identifies **what would be approved**. It is a digest anyone
with the same inputs can compute. The receipt is a separate string from
the trusted approval integration. A provider could use an opaque record
reference such as `approval-record-42`. The reference becomes evidence
of approval only when that provider confirms it corresponds to the exact
binding and is currently valid.

1. If the stored execution plan has no receipt, the controller calls
   the deployment's optional `approval_receipt(binding)` lookup.
   The core MCP submit tool accepts no receipt field. The operator
   review and approval record, if any, live in the deployment's approval
   system. SafeRemote has no built-in approval UI or record store.
2. The controller calls `ApprovalProvider.verify(receipt, binding, now)`.
   The provider decides the receipt format. For an opaque reference,
   it can fetch the record and check its reviewer, exact approved
   binding, validity and revocation. The SDK requires the provider to
   return `True`. A missing receipt yields `APPROVAL_REQUIRED`.
   A provided receipt with no verifier or a verifier that fails
   validation yields `APPROVAL_INVALID`. A verifier exception yields
   `POLICY_UNAVAILABLE`. These checks stop delivery.
3. On success, the controller replaces the cached payload with a copy
   containing the receipt in its `approval` field. It signs the full
   payload, including that receipt. The binding digest is recomputed
   when needed and has no separate field in the signed payload. The
   submit response returns delivery status, without the receipt or
   binding. If the first attempt lacked a receipt, retrying with the
   same principal, key and plan can use the same execution identity and
   binding while the original execution window is valid.
4. On admission, the endpoint rebuilds the plan from its installed
   contracts, evaluates its own policy, and recomputes the binding
   from the verified claims and its current decision. It verifies the
   receipt when its policy requires approval or the signed plan carries
   a receipt. It repeats the applicable policy and approval checks at
   each invocation, now with resolved arguments available to policy.

**Why approval is bound to these fields.** If approval were just a flag such
as “investigation approved,” the same flag could be reused for a different
user, different target, changed limit, changed export scope, or another run.
The target and arguments are covered by the plan fingerprint. The
principal, policy revision, resource list, execution ID and expiry tie the
approval to this decision and this run. Later, the endpoint calculates the
binding itself and asks its configured provider to verify the receipt.

In the successful Docker run, `owner` is allowed directly and the signed
payload has `"approval": null`. The fixture principal named `approver`
exercises the required-but-missing branch. Its submit returns
`APPROVAL_REQUIRED` before delivery. The name is a test identity, not a
reviewer role. The SDK's [approval tests](../tests/security/test_admission.py)
exercise receipt verification. See the [binding implementation](../src/remote_intent/policy.py).

## 8. Sign the execution claims

After policy and any required approval pass, the controller wraps the
compiled plan in an `ExecutionPlan`. The following JSON abbreviates the
plan and uses illustrative Unix timestamps in seconds.

```json
{
  "key_id": "fixture",
  "payload": {
    "protocol": "rir.plan/v1",
    "execution_id": "<controller-assigned ID>",
    "nonce": "<controller-assigned random value>",
    "principal": "owner",
    "issued_at": 1760000000,
    "expires_at": 1760000900,
    "policy_revision": "fixture-v1",
    "resources": ["investigation-evidence"],
    "approval": null,
    "plan": "<the full CompiledPlan object shown in step 5>"
  },
  "signature": "<Base64 representation of the Ed25519 signature>"
}
```

The controller signs the canonical UTF-8 JSON bytes of the object containing
`key_id` and `payload`. It then adds the Base64 signature and serializes the
whole envelope. **Ed25519** is the public-key signature algorithm. The
controller holds the private key. The endpoint's deployment config maps
`key_id` to a trusted public key. The public key verifies a signature but
cannot create one. The key ID is a selector in the local trust store, and
the signed object covers it as well.

**Why this structure exists.** An endpoint that received only the agent's
YAML would have no authenticated statement that the controller admitted
this principal, target and plan. An endpoint that trusted a separate
unsigned principal or expiry would let someone change those fields without
changing the plan. Signing the claims together ties the request to one
authenticated controller decision. Canonical bytes give the signer and
verifier one representation to agree on. The endpoint also rejects
duplicate JSON keys and noncanonical envelope encodings.

**Which fields do what.** The execution ID is the handle for status,
results and cancellation. The nonce is a fresh random value assigned for
this execution and checked against the endpoint's replay journal. Issued
and expiry times bound how long the decision can be used. The controller in
this release creates a 900-second validity window. The workflow timeout is
a separate execution budget. The signature authenticates the claims.
Anyone who obtains the bytes can read them. Confidentiality comes from the
transport, and Base64 is only an encoding.

See [plan encoding and verification](../src/remote_intent/security.py).

## 9. The transport adapter delivers the signed plan and scoped grant (L6)

### Why a signed plan still needs a grant for this export

The signed plan tells the **SafeRemote endpoint** that a trusted
controller admitted a specific workflow for a specific principal and
target. After its own checks, the endpoint can run the registered export
action. The grant gives that action **permission at the storage service**
to write one blob. Those are decisions made by two different services.

| Decision | Credential or evidence | Verifier |
| --- | --- | --- |
| May this machine run the approved export step for this principal? | Ed25519-signed SafeRemote plan, together with current endpoint policy and any required approval | SafeRemote endpoint |
| May this HTTPS request create or write this particular blob? | Storage-issued or storage-verifiable credential. In the fixture, a blob SAS. | Azurite, acting as the blob service |

The blob service receives an HTTPS upload with a SAS and the log bytes.
It has no SafeRemote trust store and never receives the signed plan.
Sending the plan to storage would give it no credential it recognizes.
In the other direction, possession of a valid storage SAS does not
admit a SafeRemote plan or authorize collecting logs. The endpoint still
requires its own signature, policy and runtime checks.

A grant is needed for this fixture's export because the worker has no
standing storage identity or account key. An always-on writer identity
would let the action use whatever storage permissions that identity
holds, including outside the lifetime of this execution. The fixture
instead delegates a short-lived capability for the specific object.
This reduces what a leaked credential can do. An observation-only plan
needs no storage grant. A deployment can delegate data delivery to a
separate service that enforces storage permissions through an identity
or broker.
The SafeRemote core does not require Azure, SAS, or this grant format.

### What the fixture sends and why

The controller calls `deliver_plan(target_id, envelope_bytes)` on its
`TransportAdapter`. The adapter is the interface to the configured
delivery service. Its other three methods carry status, results and
cancellation requests. The controller can make authorization decisions
without knowing how the endpoint is reached. The endpoint can verify
a plan independently of the network route that carried it.

In Docker the adapter calls a fixed endpoint address over mutual TLS.
The controller validates the endpoint's server certificate and hostname.
The endpoint listener requires a client certificate chaining to its fixture
CA. Bootstrap issues the client-auth role to the controller. This fixture
uses a private, disposable trust domain. With a shared CA, add
the deployment's intended service-identity restriction rather than
treating every certificate from the CA as authority to call the endpoint.

The HTTP request body contains these separate pieces.

| Field | Data | Who checks it |
| --- | --- | --- |
| `envelope` | Base64 of the signed execution envelope | Endpoint verifies Ed25519, target and time. |
| `grant` | Structured scope with execution ID, principal, step, target, plan fingerprint, destination, expiry, and opaque object reference | Fixture endpoint checks scope against the signed payload before accepting it for the export step. |
| `token` | A short-lived Azure Blob shared access signature (SAS) for create/write on one object | Fixture worker passes it to storage. The storage service verifies the SAS for the actual request. |

The write grant is fixture integration code. It is issued by a trusted
component of the control plane using the fixture storage account key. It
targets the specific blob path derived from the execution ID and export
step, and its scope is valid for at most two minutes and no later than the
plan expiry. The runner and endpoint do not receive the storage account
key. The endpoint checks the scope fields when preparing the grant and
rechecks them at invocation. It does not need the account key to verify
the SAS itself. The storage service has that responsibility.

Here is the handoff in order for the running example.

1. **Issue.** After the controller authorizes and signs the plan, the
   fixture transport verifies the envelope and finds the registered
   `export` step. Its issuer derives the object path
   `runs/<execution-id>/export.jsonl` using trusted configuration. The
   issuer uses the storage account key to produce a SAS for HTTPS
   create/write access to that blob. It does not grant read, list or
   delete access. It caps expiration at the earlier of two minutes
   after issuance and the plan's expiry.
2. **Bind.** The transport sends two pieces that together form the
   fixture grant. The `grant` object describes the intended use and
   the `token` field carries the secret SAS. The endpoint compares
   the scope with the verified plan and the intended step. The
   execution ID and step tie the grant to this invocation. The
   principal, target and plan fingerprint tie it to the admitted
   request. The destination must be the configured alias and the
   grant must still be in its validity window. The issuer supplies
   an opaque object reference for later lookup. The worker derives
   the actual blob path from the execution and step, rather than
   taking an upload URL from the workflow.
3. **Use.** Immediately before invoking export, the worker rechecks
   the scope and puts a `WriteLease` in `ActionContext.services`.
   The lease carries the specific blob URL with its SAS, the object
   reference, and the storage CA configuration. The export action
   opens that URL over HTTPS and uploads the bytes. Storage checks
   the SAS against the request's blob path, operation and time.
   Storage does not see the SafeRemote principal, plan fingerprint
   or approval receipt. The action also requests conditional
   creation. That is a separate protection against replacing an
   existing object. The SAS itself includes write permission.
4. **Release access separately.** The export result contains an
   object reference and integrity metadata. The writer SAS stays
   out of the result. Step 14 explains how the authorized client
   can obtain a separate read-only SAS after a successful run.

**Why check the grant twice, in two different services?** The endpoint's
checks prevent a grant intended for another run or step from being
handed to this action. The blob service's checks enforce the token
against the actual HTTP operation. In the Docker tests, a correctly
signed plan with a missing grant or wrong-step scope is rejected before
either action runs. In a different test the scope looks right, but the
SAS is expired or signed for another blob. The export action reaches
storage, which denies the upload, and no additional blob is created.
The two checks protect different boundaries.

**Why use both mutual TLS and a plan signature.** Mutual TLS authenticates
the peers and protects the header and body in transit, including the SAS.
The signature authenticates the plan claims after delivery and across
serialization or a relay. Conversely, a valid plan signature says nothing
about the confidentiality of the channel. In this fixture the grant
travels as a separate field on an authenticated, confidential connection.
It is kept out of the signed envelope, which may have a different
retention and distribution path. Keep both the transport and the grant
binding when adapting this pattern to a different delivery system.

The grant metadata travels over authenticated mTLS and is checked against
the signed claims. It is outside the Ed25519 signature. The SAS has its
own storage signature and acts as a bearer credential. Storage enforces
its object, permission and time limits, but a party that steals the SAS
can present it directly while it is valid. The local grant binding
cannot prevent that. Keep the token out of prompts, logs and durable
plan records, and use narrowly scoped, short-lived credentials.

See [adapter interface](../src/remote_intent/transports.py),
[fixture grant issuance](../tests/e2e/control_plane.py), and
[grant scope checks](../tests/e2e/delivery.py). The
[Docker runner](../tests/e2e/agent_runner.py) exercises both rejection paths.

## 10. The endpoint decides whether to admit the delivered plan (L6)

The fixture's mTLS listener bounds the request body, decodes the envelope,
verifies it, and checks that a required export grant is present and scoped
correctly. It then calls `EndpointRuntime.deliver`. The core runtime
independently performs these checks in order.

1. **Verify signature, target and freshness.** The endpoint selects the
   controller's public key from its local trust store and verifies the
   canonical signed claims. It checks that the signed target equals the
   endpoint's configured ID and that the clock is within the accepted
   issuance and expiry window. This prevents an altered plan, a plan sent
   to the wrong machine, or a stale decision from being admitted.
2. **Resolve again using local contracts.** The endpoint reconstructs the
   plan using the workflow and its installed registry, then compares the
   reconstructed fingerprint to the signed plan's fingerprint. That checks
   action versions, schemas, implementation digests, catalog and graph
   together. A legitimate controller signature cannot make different
   installed code match the intended contract. The implementation digest
   attests to deployment-supplied provenance. The deployment must ensure
   the registered digest really corresponds to the installed code.
3. **Evaluate endpoint policy.** The runtime evaluates the signed principal
   and plan using its configured policy. It compares the current revision
   and resource scope with the signed values, checks the timeout allowance,
   and verifies an approval receipt when required or present. The controller
   and endpoint are separate processes with separate policy calls. If the
   endpoint has a newer policy revision, the old decision stops here.
4. **Check the allowed effects.** The runtime admits observation by default
   when policy permits it. Export needs endpoint opt-in
   `allow_data_export=True` as well as policy permission. The runtime
   denies mutating and privileged registrations in this release. This
   ensures that even a policy configuration mistake cannot by itself
   enable an unsupported class of execution.
5. **Claim this execution in SQLite.** The journal atomically checks and
   records the execution ID, nonce, digest of the authenticated envelope
   claims and principal. A first acceptance claims the right to start the
   run. An identical admissible delivery returns the existing status.
   Reusing the ID or nonce with conflicting claims is rejected.

**Why the endpoint journal is separate from controller idempotency.**
The controller recognizes a retried user request by its submission key.
The endpoint recognizes a repeated delivery by the signed execution
identity. A delivery system can redeliver an envelope even if the user
never retried. A signature still verifies on an exact copy, so signature
verification alone cannot prevent a second execution. Persist the journal
on protected storage. On restart, the fixture's exclusive recovery marks
interrupted work for reconciliation instead of treating it as a fresh run.

See [endpoint admission](../src/remote_intent/runtime.py) and
[the replay journal](../src/remote_intent/journal.py).

### Where to look for saved state

| Point in the flow | Controller or approval system | Endpoint |
| --- | --- | --- |
| Validate or preview returns | No SDK execution record. The host can save the response for review. | No delivery or journal entry. |
| Submit stops at compilation or policy denial | The attempt creates no submission entry. An older entry for the same key can still exist. | No delivery for this attempt. |
| Submit reaches approval but stops for a missing or invalid receipt | The in-memory submission entry remains. Any reviewer decision or approval record is owned by the deployment's approval system. | No delivery for this attempt. |
| Submit signs and sends | The controller retains the payload and fingerprint in memory. Signing creates transport bytes. It does not add a durable controller record. | An inadmissible plan can be rejected before the journal claim. |
| Endpoint first admits the plan | The controller can return the adapter's status response. A lost response does not tell the controller whether the endpoint admitted it. | SQLite stores the execution ID, nonce, digest of signed claims, principal and initial `accepted` status in one transaction, alongside the admission event. |
| Endpoint runs the plan | The caller can use the execution ID to request status and projected results. | SQLite records step states, error codes, validated inline step outputs and the selected workflow result when recorded. The log bytes in this example live in the temporary artifact store and, after a successful export, in blob storage. |

The endpoint journal stores the digest of the authenticated envelope
claims for replay comparison. It does not store the full signed plan
or the approval receipt as part of the admission record. This digest
has a different purpose from the compiled plan fingerprint and the
approval binding. It identifies the claims actually delivered, including
the execution identity and its time window.

## 11. The first worker call collects logs on the machine (L7)

The scheduler waits for a step's dependencies and checks cancellation and
the remaining budget. It resolves the registered action again, builds
its strict input model, and runs endpoint step policy with the actual
arguments. Only then does it invoke the registered function through the
worker.

For `collect`, the function receives two Python objects.

- A `CollectInput` model with `fixture_id="diagnostic-sample-01"` and
  `max_records=100`.
- A trusted `ActionContext` with the execution ID, step ID, target ID,
  cooperative cancellation event and execution-local artifact store.
  The runtime constructs this context. The YAML only supplies action inputs.

The fixture function reads its fixed local evidence file, enforces a byte
limit and keeps up to the requested record count. It stores those bytes
in the run's in-memory artifact store and returns a typed `Evidence`
object. The runtime validates the declared output model before making it
available to the next step.

```json
{
  "artifact": {
    "handle": "<random 32-character hexadecimal handle>",
    "sha256": "5ab816b2fb820428ed20054088819bb6f2fe7de2d930c8ed27291acb2589dea1",
    "size": 116
  },
  "record_count": 3,
  "sha256": "5ab816b2fb820428ed20054088819bb6f2fe7de2d930c8ed27291acb2589dea1",
  "size_bytes": 116
}
```

**Why pass a handle.** The handle lets the next action find bytes inside
this execution without putting the log body in the plan, inline results,
or MCP response. Reading the handle requires a matching store entry,
digest and size in the same execution. A handle from a different
execution cannot resolve in this one. The store's lifetime ends with the
execution. A deployment that only needs collection can leave export out
and define a suitable result contract for its existing data system.

**Where the OS permissions come from.** The callable reads the file under
the worker's process identity. The bearer token and the signed plan are
application-level authorization. Neither supplies an OS credential. Give
a real collector the narrow OS permissions it needs, and isolate the
worker's filesystem, network and service credentials. The in-process
worker in the fixture shares the endpoint process privileges.

## 12. The second worker call exports the bytes to one allowed object (L7)

When `collect` finishes, the runtime replaces
`steps.collect.outputs.artifact` with the actual artifact reference and
validates the resulting `ExportInput`. It calls endpoint step policy
again with that resolved model. For example, policy can now check the
actual artifact size. At compile time it had only a reference and a
contract. This check protects the point where a valid-looking reference
becomes a concrete value that could exceed the approved limit.

The fixture worker adds the previously bound write lease to
`ActionContext.services` and invokes the export action. The action
reads the artifact through the scoped store and sends the bytes directly
to Azurite over HTTPS. The storage service validates the SAS for the exact
object and checks whether the requested operation is permitted. The action
uses conditional creation so an existing object is protected from
replacement by this request. The MCP service is outside the log data path.

**Why separate observation and export.** Permission to read local logs is
different from permission to disclose those logs to another system. This
separation is why `outsider` can request allowed observation yet cannot
run the two-step export. It also limits the credential issued for export
to one object, instead of giving the endpoint an account-wide storage key.

**What if the reply is lost?** A timeout after a write starts leaves two
possibilities. Storage may have committed the object, or it may have
received nothing. Repeating a side effect automatically could create a
second disclosure or confuse the evidence trail. For uncertain outcomes,
the runtime records `needs_reconciliation`. An operator can compare
the expected object path and stored state with the journal. The Docker
test exercises the case where the write really did commit before the
worker lost the successful outcome.

See [the artifact store](../src/remote_intent/artifacts.py),
[the endpoint worker](../tests/e2e/endpoint.py), and
[the upload implementation](../examples/action_packs/evidence_export/actions.py).

## 13. Read the status and the export receipt (L8)

Submit returns execution status containing the execution ID. In this
fixture the delivery call normally waits for execution, and the runner
also polls the status tool. A delivery integration can have different
waiting semantics. The host can ask `get_execution_status(execution_id)`
for workflow and step states or `get_execution_results(execution_id)` for
the projected result. FastMCP authenticates each call. The adapter carries
the execution ID and server-derived principal to the endpoint over its
trusted service channel. The journal scopes lookup by **both** values.
Knowing the execution ID alone provides no access. The same ownership
boundary applies to cancellation, which signals a cooperative worker and
cannot undo a side effect already committed.

A successful export action returns a typed receipt, including the
destination, internal blob name, opaque object reference, content digest,
size and storage ETag. The workflow selects four fields, and the fixture's
configured projector returns them. An abbreviated successful MCP result
looks like this.

```json
{
  "status": "ok",
  "data": {
    "evidence": "<opaque object reference>",
    "checksum": "5ab816b2fb820428ed20054088819bb6f2fe7de2d930c8ed27291acb2589dea1",
    "size": 116,
    "etag": "<storage-assigned ETag>"
  }
}
```

**Why keep this receipt.** Status answers “did the workflow finish?” The
receipt answers “which object did this execution report writing, and what
should I check when I read it?” The SHA-256 gives an expected digest of
the actual bytes. The byte count helps detect truncation. The ETag is an
opaque value returned by storage that helps identify the representation
it reported after the write. It is not used as the content hash. The
receipt is result data obtained over the authenticated service path. It
does not itself authorize download or carry a standalone signature.

## 14. Authorize read access separately and verify the downloaded bytes

The fixture adds `get_artifact_access(execution_id, object_ref)` to its
FastMCP server. The host authenticates again and sends the two
identifiers. The service checks that the principal owns the execution,
the run succeeded, the reference matches the export scope and result,
and the blob's stored metadata, size and ETag match the receipt. The
object reference selects an object within the authorized execution. It
is neither a URL nor a credential.

If the checks pass, the fixture returns a read-only SAS URL for that one
object, valid for 60 seconds. The host can keep it out of the model
context. The host connects directly to storage over HTTPS, validates the
storage certificate, and presents the SAS in the URL. Storage verifies
the token for that read. The host computes SHA-256 over the actual
download, checks its byte count, and compares its returned ETag to the
receipt. This is the first time the host downloads the log bytes.

**Why have two grants.** The endpoint needs to create or write an object.
The client needs to read a particular object after success. Sharing the
same credential would give at least one side more authority than needed.
The read SAS is still a bearer credential. Anyone who obtains it can
present it while it remains valid, so keep it out of prompts, logs, and
error messages. A deployment using its existing data retrieval service
can apply the same ownership and integrity checks there.

See [read grant issuance](../tests/e2e/control_plane.py) and
[download verification](../tests/e2e/agent_runner.py).

## What each security value proves

| Value | What it proves or identifies | Who checks it |
| --- | --- | --- |
| MCP bearer credential | Which principal this request represents after validation | Deployment's FastMCP authentication callback |
| TLS certificate and private key | Which network peer controls that key and has an accepted certificate. TLS also protects connection data. | Client for server TLS. Both services for mTLS. |
| Catalog epoch | Which snapshot of published action metadata the caller used | Controller against its current catalog. The host can also compare discovery responses. |
| Plan fingerprint | Which exact resolved workflow, actions and dependency graph are under review | Controller for retries and approval. Endpoint against its own compilation. |
| Approval receipt | The configured approval system accepted the exact binding, when policy requires approval | Configured approval provider at controller and endpoint |
| Ed25519 signature | A trusted controller signed these precise execution claims | Endpoint using the public key selected by the signed key ID |
| Execution ID, nonce and claim digest | Whether this delivery is fresh, identical to an existing execution, or conflicts with one | Endpoint journal after admission checks |
| Write or read SAS | Permission for a storage operation on a specific object within the token's lifetime | Blob storage service |
| Export receipt | Which object the execution reported writing and what content digest, size and ETag to expect | Controller before issuing read access, then host against the actual download |

The live connections in this fixture use TLS. The Ed25519 signature and
SHA-256 digests are readable verification data and do not encrypt the
payload. The core provides no at-rest encryption. Configure encrypted
volumes, storage, backups and log retention at deployment time.
Treat bearer credentials, controller private keys, TLS private keys and
storage issuer credentials as secrets with separate purposes. In Docker,
the controller's plan key also signs the optional demonstration catalog.
A deployment can use independent catalog and plan signing lifecycles.

## Run it and defend it in review

From the repository root, run Docker in Linux-container mode. The agent,
control plane, endpoint and Azurite communicate on internal Compose
networks. The endpoint image contains the fixed evidence file. Bootstrap
generates disposable credentials. The controller and endpoint install
the SDK from the built wheel.

```powershell
docker compose -f tests/e2e/docker-compose.yml --profile e2e up --build --abort-on-container-exit --exit-code-from e2e-runner
docker compose -f tests/e2e/docker-compose.yml --profile e2e down -v --remove-orphans
```

Read the runner output before running the second command, which removes
fixture volumes. Expect two invocations, one blob, three records and
116 bytes from the successful scenario. The runner also exercises
authentication, denial without action effects, approval required,
signature and catalog mismatch, replay, storage permissions, and
post-write uncertainty. For a Python-only integration, the
[complete demo](../examples/demo.py) wires `ActionRegistry`,
`WorkflowCompiler`, `Controller`, `EndpointRuntime` and `FakeTransport`
without FastMCP. For a deployed integration, configure authentication,
registered actions, policy, worker isolation, and delivery at the
boundaries described above. Add a data integration if the workflow needs
external export. Validate the configured identity, certificates, OS
permissions, storage authorization and recovery behavior.

A useful way to present this is to pick three failure points and follow
them to the component that stops the request. Change the catalog during
discovery and the controller rejects the old epoch. Change the
destination in transit and the endpoint rejects the signature. Replay a
previously admitted delivery and the journal prevents a second worker
call. These failures exercise distinct boundaries.

Related reading includes the [SDK reference](SDK.md), the
[Docker runbook](DOCKER_E2E_EXTERNAL_EXPORT.md), the
[validation guide](VALIDATION.md), and the
[endpoint isolation decision](adr/0001-endpoint-isolation.md).
