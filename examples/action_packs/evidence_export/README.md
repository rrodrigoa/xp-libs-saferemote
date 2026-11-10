# Evidence export action pack

This is an example publisher-owned pack. Install the SDK with the optional
`e2e` extra to run it with the Docker fixture.

- `collect` reads a bounded endpoint-local fixture and creates an
  execution-scoped artifact.
- `upload_blob` accepts that typed artifact and an allowlisted destination
  alias. The fixture worker supplies a short-lived write lease through
  `ActionContext.services` after validating its execution and step binding.
- The action performs a conditional create and returns a receipt containing
  the blob identity, size, digest and ETag. An independent read grant and
  download verify the receipt in the end-to-end test.

Publisher code owns destination semantics and must enforce classification,
scope and size. The YAML compiler sees ordinary named actions and typed
arguments. The [example workflow](workflow.yaml) shows their composition.
The [Docker runbook](../../../docs/DOCKER_E2E_EXTERNAL_EXPORT.md) explains
the fixture and its operational limits.
