"""Ed25519 envelope signing and independent verification."""

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import Field, ValidationError

from .compiler import CompiledPlan
from .contracts import Contract, Identifier, canonical, digest
from .errors import IntentError


class ExecutionPlan(Contract):
    protocol: Literal["rir.plan/v1"] = "rir.plan/v1"
    execution_id: Identifier
    nonce: Identifier
    principal: Annotated[str, Field(min_length=1, max_length=256)]
    issued_at: int
    expires_at: int
    policy_revision: str
    resources: list[str]
    approval: str | None = None
    plan: CompiledPlan


class SignedEnvelope(Contract):
    key_id: Identifier
    payload: ExecutionPlan
    signature: str


class Signer:
    """Trusted private signing key held by the controller."""

    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        self.key_id = key_id
        self._private_key = private_key

    def sign(self, payload: ExecutionPlan) -> bytes:
        claims = {
            "key_id": self.key_id,
            "payload": payload.model_dump(mode="json", by_alias=True),
        }
        signature = base64.b64encode(self._private_key.sign(canonical(claims))).decode(
            "ascii"
        )
        return canonical({**claims, "signature": signature})


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class TrustStore:
    """Current authorized signing keys. Rotation replaces this trusted mapping."""

    def __init__(self, keys: Mapping[str, Ed25519PublicKey]) -> None:
        self._keys = dict(keys)

    def verify(
        self, raw: bytes, *, now: int, target_id: str
    ) -> tuple[ExecutionPlan, str]:
        if len(raw) > 1_048_576:
            raise IntentError("RESOURCE_QUOTA_EXCEEDED", resource="envelope bytes")
        try:
            envelope = SignedEnvelope.model_validate_json(raw)
            data = json.loads(raw, object_pairs_hook=_unique)
            if (
                canonical(data) != raw
                or canonical(envelope.model_dump(mode="json", by_alias=True)) != raw
            ):
                raise ValueError
            signature = base64.b64decode(data.pop("signature"), validate=True)
            key = self._keys.get(envelope.key_id)
            if key is None:
                raise IntentError("TRUST_STORE_MISMATCH")
            key.verify(signature, canonical(data))
        except (
            ValidationError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
            binascii.Error,
            InvalidSignature,
        ):
            raise IntentError("PLAN_SIGNATURE_INVALID") from None
        plan = envelope.payload
        if plan.plan.workflow.spec.target.device_id != target_id:
            raise IntentError("TARGET_MISMATCH")
        if (
            not plan.issued_at - 5 <= now < plan.expires_at
            or not 0 < plan.expires_at - plan.issued_at <= 900
        ):
            raise IntentError("PLAN_EXPIRED")
        return plan, digest(data)
