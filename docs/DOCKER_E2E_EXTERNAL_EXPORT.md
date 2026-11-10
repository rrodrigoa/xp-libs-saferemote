# Docker end-to-end export fixture

Use Linux containers with Docker Compose. From the repository root run:

```powershell
docker compose -f tests/e2e/docker-compose.yml --profile e2e up --build --abort-on-container-exit --exit-code-from e2e-runner
docker compose -f tests/e2e/docker-compose.yml --profile e2e down -v --remove-orphans
```

The command runs a deterministic agent against FastMCP over HTTPS. The
controller and endpoint install the SDK from the Docker-built wheel. Only
the endpoint image contains the evidence file. Bootstrap generates disposable
role certificates, signing keys, fixture identities and an emulator account
key. The endpoint and runner receive neither the signing private key nor the
storage account key. All network links are internal and no ports are published.

The expected output reports the verified SHA-256, byte count, two endpoint
invocations and one blob. The runner checks that validation, preview, invalid
destinations and unauthorized submissions produce zero invocations and blobs.
Read delegation requires successful execution, ownership and the matching
object reference. Blob permissions are checked against Azurite itself.

If readiness fails, check Docker is running in Linux mode and can pull the
pinned base images. Inspect service status with `docker compose ps -a` using
the same file and profile. Use the down command before retrying. It removes
the journal, report and credential volumes. Run the fixture in a disposable
environment. The example's SAS issuer and the authenticated diagnostic tools
are fixture integrations and are unsuitable for production deployment.

Azurite exercises service SAS and the Azure Blob protocol. Production storage
identity, revocation, networking and governance require separate verification.
A short-lived SAS is a bearer credential. The export worker uses HTTPS and
conditional creation, then the runner reads back the actual bytes using its
own delegated access. The fixture never moves the artifact through MCP.
