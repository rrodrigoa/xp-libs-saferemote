"""Run FastMCP and the real controller with fixture transport and SAS issuance."""

import asyncio
import base64
import hmac
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

import uvicorn
from azure.core.exceptions import HttpResponseError, ResourceExistsError
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastmcp import Context
from fastmcp.server.dependencies import get_http_headers

from examples.action_packs.evidence_export.actions import (
    API_VERSION,
    DESTINATION,
    EXPORT,
    registry,
)
from remote_intent import WorkflowCompiler
from remote_intent.catalog import publish_catalog
from remote_intent.contracts import digest
from remote_intent.controller import Controller
from remote_intent.errors import IntentError
from remote_intent.mcp import create_server
from remote_intent.security import Signer, TrustStore

from .common import (
    ACCOUNT,
    CA_FILE,
    CONTAINER,
    PRINCIPALS,
    SECRETS,
    STORAGE_URL,
    TARGET,
    FixturePolicy,
    blob_name,
)
from .delivery import EndpointClient, Grant

Probe = Literal[
    "signature",
    "target",
    "digest",
    "missing",
    "wrong-step",
    "expired",
    "wrong-blob",
    "lost-receipt",
]
_probe: ContextVar[Probe | None] = ContextVar("fixture_probe", default=None)


class FixtureTransport:
    def __init__(self, trust: TrustStore, signer: Signer, key: str) -> None:
        self.client = EndpointClient()
        self.trust = trust
        self.signer = signer
        self.key = key
        self.scopes: dict[str, Grant] = {}

    def sas(self, path: str, *, write: bool, expires: int) -> str:
        return generate_blob_sas(
            ACCOUNT,
            CONTAINER,
            path,
            account_key=self.key,
            permission=BlobSasPermissions(create=write, write=write, read=not write),
            expiry=datetime.fromtimestamp(expires, UTC),
            protocol="https",
        )

    async def deliver_plan(self, target_id: str, envelope: bytes) -> dict[str, Any]:
        now = int(time.time())
        payload, _ = self.trust.verify(envelope, target_id=target_id, now=now)
        mode = _probe.get()
        if mode == "signature":
            envelope = envelope.replace(
                b'"principal":"owner"', b'"principal":"outsider"'
            )
        elif mode == "target":
            payload.plan.workflow.spec.target.device_id = "wrong-endpoint"
            envelope = self.signer.sign(payload)
        elif mode == "digest":
            next(iter(payload.plan.actions.values())).implementation_digest = "0" * 64
            envelope = self.signer.sign(payload)
        body = {"envelope": base64.b64encode(envelope).decode()}
        for step, spec in payload.plan.actions.items():
            if spec.uses == EXPORT and mode != "missing":
                scope = self.scopes.get(payload.execution_id) or Grant(
                    execution_id=payload.execution_id,
                    principal=payload.principal,
                    step_id=step,
                    target_id=target_id,
                    plan_digest=payload.plan.fingerprint,
                    destination=DESTINATION,
                    expires_at=min(now + 120, payload.expires_at),
                    object_ref=digest([payload.execution_id, step]),
                )
                if mode in {None, "lost-receipt"}:
                    self.scopes[payload.execution_id] = scope
                scope_dict = scope.model_dump()
                if mode == "wrong-step":
                    scope_dict["step_id"] = "other-step"
                body["grant"] = scope_dict
                body["token"] = self.sas(
                    blob_name(
                        payload.execution_id,
                        "other-step" if mode == "wrong-blob" else step,
                    ),
                    write=True,
                    expires=now - 60 if mode == "expired" else scope.expires_at,
                )
        if mode == "lost-receipt":
            body["simulate_lost_receipt"] = True
        token = body.get("token")
        if self.key.encode() in envelope or (token and token.encode() in envelope):
            raise IntentError("SECURITY_DENIED")
        return await self.client.request("deliver", **body)

    async def status(self, execution_id: str, principal: str) -> dict[str, Any]:
        return await self.client.request(
            "status", execution_id=execution_id, principal=principal
        )

    async def results(self, execution_id: str, principal: str) -> dict[str, Any]:
        return await self.client.request(
            "results", execution_id=execution_id, principal=principal
        )

    async def request_cancel(self, execution_id: str, principal: str) -> bool:
        reply = await self.client.request(
            "cancel", execution_id=execution_id, principal=principal
        )
        return reply["requested"] is True


def main():
    key = (SECRETS / "storage.key").read_text()
    private_key = Ed25519PrivateKey.from_private_bytes(
        (SECRETS / "plan.key").read_bytes()
    )
    tokens = {name: f"Bearer {(SECRETS / name).read_text()}" for name in PRINCIPALS}
    signer = Signer("fixture", private_key)
    transport = FixtureTransport(
        TrustStore({"fixture": private_key.public_key()}), signer, key
    )
    storage = BlobServiceClient(
        STORAGE_URL,
        credential={"account_name": ACCOUNT, "account_key": key},
        api_version=API_VERSION,
        connection_verify=CA_FILE,
        retry_total=0,
        connection_timeout=3,
        read_timeout=5,
    )
    container = storage.get_container_client(CONTAINER)
    for attempt in range(30):
        try:
            container.create_container()
            break
        except ResourceExistsError:
            break
        except Exception:
            if attempt == 29:
                raise RuntimeError("Fixture storage did not initialize") from None
            time.sleep(1)
    controller = Controller(
        target_id=TARGET,
        compiler=WorkflowCompiler(registry(), "linux"),
        signer=signer,
        transport=transport,
        policy=FixturePolicy(),
        visible=lambda principal, target, uses: principal in PRINCIPALS,
    )

    async def authenticate(ctx: Context) -> str | None:
        supplied = (
            get_http_headers(include={"authorization"})
            .get("authorization", "")
            .encode()
        )
        if len(supplied) > 256:
            return None
        return next(
            (
                name
                for name, token in tokens.items()
                if hmac.compare_digest(supplied, token.encode())
            ),
            None,
        )

    server = create_server(controller, authenticate)

    @server.tool
    async def fixture_catalog(ctx: Context) -> dict[str, Any]:
        """Publish a signed catalog to the fixture's authorized observer."""
        if await authenticate(ctx) != "owner":
            return IntentError("SECURITY_DENIED").envelope()
        data, signature = publish_catalog(
            controller.compiler.registry,
            "linux",
            TARGET,
            int(time.time()) + 120,
            private_key,
        )
        return {
            "status": "ok",
            "data": {
                "catalog": base64.b64encode(data).decode(),
                "signature": base64.b64encode(signature).decode(),
            },
        }

    @server.tool
    async def get_artifact_access(
        execution_id: str, object_ref: str, ctx: Context
    ) -> dict[str, Any]:
        """Fixture-only explicit read delegation. The returned URL is a secret."""
        try:
            principal = await authenticate(ctx)
            if principal is None:
                raise IntentError("MCP_SESSION_UNAUTHENTICATED")
            status = await transport.status(execution_id, principal)
            scope = transport.scopes.get(execution_id)
            result = await transport.results(execution_id, principal)
            if (
                status["status"] != "succeeded"
                or scope is None
                or object_ref != scope.object_ref
                or result.get("evidence") != object_ref
            ):
                raise IntentError("ARTIFACT_SCOPE_DENIED")
            path = blob_name(execution_id, scope.step_id)
            properties = await asyncio.to_thread(
                container.get_blob_client(path).get_blob_properties
            )
            if (
                properties.metadata.get("sha256") != result.get("checksum")
                or properties.size != result.get("size")
                or properties.etag != result.get("etag")
            ):
                raise IntentError("ARTIFACT_DIGEST_MISMATCH")
            token = transport.sas(path, write=False, expires=int(time.time()) + 60)
            return {
                "status": "ok",
                "data": {"read_url": f"{STORAGE_URL}/{CONTAINER}/{path}?{token}"},
            }
        except IntentError as exc:
            return exc.envelope()
        except Exception:
            return IntentError("ARTIFACT_SCOPE_DENIED").envelope()

    @server.tool
    async def fixture_probe(
        workflow_yaml: str, scenario: Probe, ctx: Context
    ) -> dict[str, Any]:
        """Inject a bounded fixture fault into an otherwise real signed dispatch."""
        if await authenticate(ctx) != "owner":
            return IntentError("SECURITY_DENIED").envelope()
        marker = _probe.set(scenario)
        try:
            plan = controller.compile("owner", workflow_yaml)
            result = await controller.submit("owner", plan, f"probe-{scenario}")
            return {"status": "ok", "data": result}
        except IntentError as exc:
            return exc.envelope()
        finally:
            _probe.reset(marker)

    @server.tool
    async def fixture_audit(ctx: Context) -> dict[str, Any]:
        """Return sanitized execution, issuance and blob counters."""
        if await authenticate(ctx) != "owner":
            return IntentError("SECURITY_DENIED").envelope()
        endpoint = await transport.client.request("audit")
        count = await asyncio.to_thread(lambda: sum(1 for _ in container.list_blobs()))
        return {
            "status": "ok",
            "data": {**endpoint, "blobs": count, "grant_scopes": len(transport.scopes)},
        }

    @server.tool
    async def fixture_check_writer(execution_id: str, ctx: Context) -> dict[str, Any]:
        """Check the writer's permission boundary without returning a credential."""
        if await authenticate(ctx) != "owner":
            return IntentError("SECURITY_DENIED").envelope()
        from azure.storage.blob import BlobClient

        scope = transport.scopes[execution_id]
        path = blob_name(execution_id, scope.step_id)
        token = transport.sas(path, write=True, expires=scope.expires_at)
        with BlobClient.from_blob_url(
            f"{STORAGE_URL}/{CONTAINER}/{path}?{token}",
            api_version=API_VERSION,
            connection_verify=CA_FILE,
            retry_total=0,
        ) as blob:
            for operation in (blob.get_blob_properties, blob.delete_blob):
                try:
                    await asyncio.to_thread(operation)
                except HttpResponseError as exc:
                    if exc.status_code != 403:
                        raise RuntimeError(
                            "Unexpected storage permission result"
                        ) from None
                else:
                    raise RuntimeError("Writer exceeded its storage permissions")
        return {"status": "ok", "data": {"write_only": True}}

    uvicorn.run(
        server.http_app(stateless_http=True),
        host="0.0.0.0",
        port=8443,
        ssl_certfile=str(SECRETS / "tls.pem"),
        ssl_keyfile=str(SECRETS / "tls.key"),
        access_log=False,
        log_level="critical",
    )


if __name__ == "__main__":
    main()
