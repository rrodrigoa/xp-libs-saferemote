"""Versioned wire contracts and canonical encoding."""

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Seconds = Annotated[int, Field(gt=0, le=3600)]


class Contract(BaseModel):
    """Strict contract shared by SDK wire models."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Target(Contract):
    device_id: Identifier


class Metadata(Contract):
    name: Identifier


class Limits(Contract):
    timeout_seconds: Seconds = 120


class Step(Contract):
    id: Identifier
    uses: Annotated[str, Field(min_length=1, max_length=128)]
    arguments: dict[str, Any] = Field(alias="with")
    depends_on: list[Identifier] = Field(default_factory=list, max_length=64)
    when: dict[str, Any] | None = None
    after: dict[Identifier, list[Literal["succeeded", "failed", "skipped"]]] = Field(
        default_factory=dict
    )


class WorkflowSpec(Contract):
    target: Target
    steps: list[Step] = Field(min_length=1, max_length=64)
    outputs: dict[Identifier, dict[str, Any]] = Field(
        default_factory=dict, max_length=64
    )
    limits: Limits = Field(default_factory=Limits)
    request_reason: Annotated[str, Field(max_length=512)] | None = None


class Workflow(Contract):
    apiVersion: Literal["remote.intent/v1"]
    kind: Literal["Workflow"]
    metadata: Metadata
    spec: WorkflowSpec


class ArtifactRef(Contract):
    handle: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    sha256: Digest
    size: Annotated[int, Field(ge=0)]


def canonical(value: Any) -> bytes:
    """Encode canonical SDK JSON (UTF-8, sorted keys, finite numbers)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    """Return SHA-256 of the canonical encoding."""
    return hashlib.sha256(canonical(value)).hexdigest()
