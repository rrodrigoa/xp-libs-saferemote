"""Confidential fixture transport and an execution-bound credential lease."""

import asyncio
import json
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.error import URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from examples.action_packs.evidence_export.actions import DESTINATION, WriteLease
from remote_intent.contracts import Contract, Digest, Identifier, canonical
from remote_intent.errors import IntentError, get_error_info
from remote_intent.security import ExecutionPlan

from .common import CA_FILE, CONTAINER, SECRETS, STORAGE_URL, blob_name


class Grant(Contract):
    execution_id: Identifier
    principal: Identifier
    step_id: Identifier
    target_id: Identifier
    plan_digest: Digest
    destination: str
    expires_at: int
    object_ref: Identifier


@dataclass(frozen=True, slots=True)
class ProvisionedGrant:
    scope: Grant
    token: str = field(repr=False)

    def lease(self, payload: ExecutionPlan, step_id: str, now: int) -> WriteLease:
        scope = self.scope
        if (
            scope.execution_id != payload.execution_id
            or scope.principal != payload.principal
            or scope.target_id != payload.plan.workflow.spec.target.device_id
            or scope.plan_digest != payload.plan.fingerprint
            or scope.step_id != step_id
            or scope.destination != DESTINATION
            or not now < scope.expires_at <= payload.expires_at
            or not 0 < len(self.token) <= 2048
        ):
            raise IntentError("SECURITY_DENIED")
        path = blob_name(scope.execution_id, scope.step_id)
        return WriteLease(
            path,
            scope.object_ref,
            f"{STORAGE_URL}/{CONTAINER}/{path}?{self.token}",
            CA_FILE,
        )


class EndpointClient:
    """Fixture-only mTLS client for the fixed delivery service."""

    def __init__(self) -> None:
        context = ssl.create_default_context(cafile=CA_FILE)
        context.load_cert_chain(SECRETS / "tls.pem", SECRETS / "tls.key")
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))

    def _request(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"https://endpoint:8443/{operation}",
            data=canonical(body),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=135) as response:
                result = json.loads(response.read(1_048_577))
        except (URLError, TimeoutError, ValueError):
            raise IntentError("INTERNAL_ERROR") from None
        if result["status"] == "error":
            code = result["error"]["code"]
            get_error_info(code)
            raise IntentError(code)
        return result["data"]

    async def request(self, operation: str, **body: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, operation, body)
