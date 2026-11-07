"""Signed publisher catalog snapshots for trusted target inventory."""

import json
from collections.abc import Mapping
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import ValidationError

from .actions import ActionRegistry, ActionSpec
from .contracts import Contract, Digest, Identifier, canonical, digest
from .errors import IntentError
from .security import _unique

_DOMAIN = b"rir.catalog/v1\x00"


class CatalogSnapshot(Contract):
    """Exact published contracts for one target and limited validity window."""

    protocol: Literal["rir.catalog/v1"] = "rir.catalog/v1"
    target_id: Identifier
    epoch: Digest
    expires_at: int
    actions: list[ActionSpec]


def publish_catalog(
    registry: ActionRegistry,
    platform: str,
    target_id: str,
    expires_at: int,
    publisher_key: Ed25519PrivateKey,
) -> tuple[bytes, bytes]:
    """Derive and sign a catalog snapshot from trusted registrations."""
    actions = registry.catalog(platform)
    epoch = digest([spec.model_dump(mode="json") for spec in actions])
    snapshot = CatalogSnapshot(
        target_id=target_id, epoch=epoch, expires_at=expires_at, actions=actions
    )
    data = canonical(snapshot.model_dump(mode="json"))
    if len(data) > 1_048_576:
        raise IntentError("RESOURCE_QUOTA_EXCEEDED", resource="catalog bytes")
    return data, publisher_key.sign(_DOMAIN + data)


def verify_catalog(
    data: bytes,
    signature: bytes,
    *,
    publisher_key: Ed25519PublicKey,
    target_id: str,
    now: int,
) -> CatalogSnapshot:
    """Authenticate a snapshot before trusting its target and exact action metadata."""
    if len(data) > 1_048_576:
        raise IntentError("RESOURCE_QUOTA_EXCEEDED", resource="catalog bytes")
    try:
        publisher_key.verify(signature, _DOMAIN + data)
        parsed = json.loads(data, object_pairs_hook=_unique)
        snapshot = CatalogSnapshot.model_validate_json(data)
        if (
            canonical(parsed) != data
            or canonical(snapshot.model_dump(mode="json")) != data
        ):
            raise ValueError
    except (ValidationError, InvalidSignature, ValueError, RecursionError):
        raise IntentError("ACTION_PACK_UNTRUSTED") from None
    if snapshot.target_id != target_id:
        raise IntentError("TARGET_MISMATCH")
    if now >= snapshot.expires_at:
        raise IntentError("CATALOG_STALE")
    if snapshot.epoch != digest(
        [spec.model_dump(mode="json") for spec in snapshot.actions]
    ):
        raise IntentError("ACTION_SCHEMA_MISMATCH")
    return snapshot


def verify_installed_actions(
    snapshot: CatalogSnapshot, registry: ActionRegistry, platform: str
) -> None:
    """Require exact installed contract parity with an authenticated snapshot."""
    installed: Mapping[str, ActionSpec] = {
        spec.uses: spec for spec in registry.catalog(platform)
    }
    published = {spec.uses: spec for spec in snapshot.actions}
    if len(published) != len(snapshot.actions) or installed != published:
        raise IntentError("ACTION_SCHEMA_MISMATCH")
