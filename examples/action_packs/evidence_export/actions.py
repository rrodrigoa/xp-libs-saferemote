"""Consumer-owned actions for the isolated evidence export example."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from azure.core.exceptions import AzureError, HttpResponseError
from azure.storage.blob import BlobClient
from pydantic import Field

from remote_intent import ActionContext, ActionRegistry, ArtifactRef, Effect, action
from remote_intent.contracts import Contract, Digest
from remote_intent.errors import IntentError

DESTINATION = "investigation-evidence"
COLLECT = "test.evidence.collect@1"
EXPORT = "test.evidence.upload_blob@1"
API_VERSION = "2025-11-05"
MAX_BYTES = 65_536


class CollectInput(Contract):
    fixture_id: Literal["diagnostic-sample-01"]
    max_records: Annotated[int, Field(ge=1, le=1000)]


class Evidence(Contract):
    artifact: ArtifactRef
    record_count: int
    sha256: Digest
    size_bytes: int


class ExportInput(Contract):
    source: ArtifactRef
    destination: Literal["investigation-evidence"]


class Receipt(Contract):
    destination: Literal["investigation-evidence"]
    object_ref: str
    blob_name: str
    content_sha256: Digest
    size_bytes: int
    etag: str


@dataclass(frozen=True, slots=True)
class WriteLease:
    blob_name: str
    object_ref: str
    url: str = field(repr=False)
    ca_file: str


@action(
    key="test.evidence.collect",
    version="1.0.0",
    description="Collect bounded synthetic diagnostics",
    effect=Effect.OBSERVE,
    input_model=CollectInput,
    output_model=Evidence,
    examples=[{"fixture_id": "diagnostic-sample-01", "max_records": 100}],
)
def collect(ctx: ActionContext, args: CollectInput) -> Evidence:
    path = Path("/evidence/diagnostic-sample-01.jsonl")
    with path.open("rb") as stream:
        records = stream.read(MAX_BYTES + 1)
    if len(records) > MAX_BYTES:
        raise IntentError("RESOURCE_QUOTA_EXCEEDED", resource="evidence bytes")
    lines = records.splitlines(keepends=True)[: args.max_records]
    artifact = ctx.artifacts.put(b"".join(lines), sensitivity="synthetic")
    return Evidence(
        artifact=artifact,
        record_count=len(lines),
        sha256=artifact.sha256,
        size_bytes=artifact.size,
    )


@action(
    key="test.evidence.upload_blob",
    version="1.0.0",
    description="Disclose synthetic evidence to the allowlisted investigation destination",
    effect=Effect.DATA_EXPORT,
    input_model=ExportInput,
    output_model=Receipt,
)
def export(ctx: ActionContext, args: ExportInput) -> Receipt:
    lease = ctx.services
    if not isinstance(lease, WriteLease):
        raise IntentError("SECURITY_DENIED")
    data = ctx.artifacts.read(args.source)
    if len(data) > MAX_BYTES:
        raise IntentError("RESOURCE_QUOTA_EXCEEDED", resource="evidence bytes")
    try:
        with BlobClient.from_blob_url(
            lease.url,
            api_version=API_VERSION,
            connection_verify=lease.ca_file,
            retry_total=0,
            connection_timeout=3,
            read_timeout=5,
        ) as blob:
            reply = blob.upload_blob(
                data,
                overwrite=False,
                validate_content=True,
                metadata={"sha256": args.source.sha256},
            )
    except HttpResponseError as exc:
        if exc.status_code in {401, 403}:
            raise IntentError("SECURITY_DENIED") from None
        raise IntentError("RECONCILIATION_REQUIRED", action=EXPORT) from None
    except AzureError:
        raise IntentError("RECONCILIATION_REQUIRED", action=EXPORT) from None
    return Receipt(
        destination=args.destination,
        object_ref=lease.object_ref,
        blob_name=lease.blob_name,
        content_sha256=args.source.sha256,
        size_bytes=len(data),
        etag=reply["etag"],
    )


def registry() -> ActionRegistry:
    from hashlib import sha256

    registered = ActionRegistry()
    pack_digest = sha256(Path(__file__).read_bytes()).hexdigest()
    for fn in (collect, export):
        registered.install(fn, implementation_digest=pack_digest)
    return registered
