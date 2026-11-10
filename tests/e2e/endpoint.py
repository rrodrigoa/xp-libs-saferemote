"""Run the actual SafeRemote endpoint over a fixture-only mTLS listener."""

import base64
import json
import ssl
import time
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from examples.action_packs.evidence_export.actions import EXPORT, registry
from remote_intent import EndpointRuntime, WorkflowCompiler
from remote_intent.errors import IntentError
from remote_intent.journal import Journal
from remote_intent.runtime import InProcessReadOnlyWorker
from remote_intent.security import TrustStore

from .common import CA_FILE, SECRETS, TARGET, FixturePolicy
from .delivery import Grant, ProvisionedGrant

_active = ContextVar("fixture_grant", default=None)
_post_commit_probe = ContextVar("post_commit_probe", default=False)


class FixtureWorker(InProcessReadOnlyWorker):
    async def invoke(self, action, context, arguments, timeout):
        if action.spec.uses == EXPORT:
            active = _active.get()
            if active is None or active[0].execution_id != context.execution_id:
                raise IntentError("SECURITY_DENIED")
            payload, grant = active
            lease = grant.lease(payload, context.step_id, int(time.time()))
            context = replace(context, services=lease)
            result = await super().invoke(action, context, arguments, timeout)
            if _post_commit_probe.get():
                raise IntentError("RECONCILIATION_REQUIRED", action=EXPORT)
            return result
        return await super().invoke(action, context, arguments, timeout)


def main():
    trust = TrustStore(
        {
            "fixture": Ed25519PublicKey.from_public_bytes(
                (SECRETS / "plan.pub").read_bytes()
            )
        }
    )
    journal = Journal(Path("/journal/endpoint.sqlite"))
    journal.recover_exclusive()
    endpoint = EndpointRuntime(
        target_id=TARGET,
        compiler=WorkflowCompiler(registry(), "linux"),
        trust=trust,
        journal=journal,
        policy=FixturePolicy(),
        worker=FixtureWorker(),
        allow_data_export=True,
        result_projection=lambda principal, output: output,
    )

    async def dispatch(request: Request):
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1_420_000:
                    raise IntentError(
                        "RESOURCE_QUOTA_EXCEEDED", resource="delivery bytes"
                    )
            data = json.loads(body)
            operation = request.path_params["operation"]
            if operation == "deliver":
                raw = base64.b64decode(data["envelope"], validate=True)
                payload, _ = trust.verify(raw, now=int(time.time()), target_id=TARGET)
                export_steps = [
                    step
                    for step, spec in payload.plan.actions.items()
                    if spec.uses == EXPORT
                ]
                active = None
                if export_steps:
                    if len(export_steps) != 1 or data.get("grant") is None:
                        raise IntentError("SECURITY_DENIED")
                    grant = ProvisionedGrant(
                        Grant.model_validate(data["grant"]), data["token"]
                    )
                    grant.lease(payload, export_steps[0], int(time.time()))
                    active = payload, grant
                marker = _active.set(active)
                probe = _post_commit_probe.set(
                    data.get("simulate_lost_receipt") is True
                )
                try:
                    result = await endpoint.deliver(raw)
                finally:
                    _post_commit_probe.reset(probe)
                    _active.reset(marker)
            elif operation == "audit":
                with journal.connection() as db:
                    result = {
                        "executions": db.execute(
                            "SELECT count(*) FROM executions"
                        ).fetchone()[0],
                        "invocations": db.execute(
                            "SELECT count(*) FROM events WHERE step_id IS NOT NULL AND state='running'"
                        ).fetchone()[0],
                    }
            else:
                execution_id, principal = data["execution_id"], data["principal"]
                if operation == "status":
                    result = journal.status(execution_id, principal)
                elif operation == "results":
                    result = endpoint.results(execution_id, principal)
                elif operation == "cancel":
                    result = {
                        "requested": endpoint.request_cancel(execution_id, principal)
                    }
                else:
                    raise IntentError("SECURITY_DENIED")
            return JSONResponse({"status": "ok", "data": result})
        except IntentError as exc:
            return JSONResponse(exc.envelope())
        except Exception:
            return JSONResponse(IntentError("INTERNAL_ERROR").envelope())

    uvicorn.run(
        Starlette(routes=[Route("/{operation}", dispatch, methods=["POST"])]),
        host="0.0.0.0",
        port=8443,
        ssl_certfile=str(SECRETS / "tls.pem"),
        ssl_keyfile=str(SECRETS / "tls.key"),
        ssl_ca_certs=CA_FILE,
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        access_log=False,
        log_level="critical",
    )


if __name__ == "__main__":
    main()
