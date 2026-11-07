"""Execution-scoped data artifacts for trusted in-process workers."""

import hashlib
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any
from uuid import uuid4

from .contracts import ArtifactRef, canonical
from .errors import IntentError


@dataclass(frozen=True, slots=True)
class _Artifact:
    data: bytes
    reference: ArtifactRef
    expires: float
    sensitivity: str


class ArtifactScope:
    """Bounded, ephemeral storage that only the owning execution can access."""

    def __init__(self, quota: int = 1_048_576, ttl: int = 900) -> None:
        self._quota = quota
        self._ttl = ttl
        self._artifacts: dict[str, _Artifact] = {}
        self._lock = Lock()

    def put(self, data: bytes, *, sensitivity: str = "internal") -> ArtifactRef:
        with self._lock:
            now = time.monotonic()
            self._artifacts = {
                key: value
                for key, value in self._artifacts.items()
                if value.expires > now
            }
            if (
                len(self._artifacts) >= 128
                or len(data) + sum(len(item.data) for item in self._artifacts.values())
                > self._quota
            ):
                raise IntentError(
                    "RESOURCE_QUOTA_EXCEEDED", resource="artifact storage"
                )
            handle = uuid4().hex
            reference = ArtifactRef(
                handle=handle, size=len(data), sha256=hashlib.sha256(data).hexdigest()
            )
            self._artifacts[handle] = _Artifact(
                bytes(data), reference.model_copy(), now + self._ttl, sensitivity
            )
            return reference

    async def put_json(
        self, value: Any, *, sensitivity: str = "internal"
    ) -> ArtifactRef:
        return self.put(canonical(value), sensitivity=sensitivity)

    def read(self, reference: ArtifactRef) -> bytes:
        with self._lock:
            artifact = self._artifacts.get(reference.handle)
            if artifact is None or artifact.reference != reference:
                raise IntentError("ARTIFACT_SCOPE_DENIED")
            if artifact.expires <= time.monotonic():
                del self._artifacts[reference.handle]
                raise IntentError("ARTIFACT_EXPIRED")
            return artifact.data
