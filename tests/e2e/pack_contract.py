"""Focused pack failure checks supplement the real network scenario."""

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from azure.core.exceptions import ServiceResponseError

from examples.action_packs.evidence_export import actions
from remote_intent import ActionContext, IntentError
from remote_intent.artifacts import ArtifactScope


def assert_error(code, operation, *args):
    try:
        operation(*args)
    except IntentError as exc:
        assert exc.code == code
        assert "synthetic-secret" not in str(exc.envelope())
    else:
        raise AssertionError("Expected a typed action failure")


def run_pack_contract():
    artifacts = ArtifactScope()
    context = ActionContext("test", "export", "fixture", Event(), artifacts=artifacts)
    inputs = actions.ExportInput(
        source=artifacts.put(b"test"), destination=actions.DESTINATION
    )
    assert_error("SECURITY_DENIED", actions.export, context, inputs)
    lease = actions.WriteLease(
        "blob", "ref", "https://invalid.example?sig=synthetic-secret", "ca.pem"
    )
    context = ActionContext(
        "test", "export", "fixture", Event(), services=lease, artifacts=artifacts
    )
    oversized = inputs.model_copy(
        update={"source": artifacts.put(b"x" * (actions.MAX_BYTES + 1))}
    )
    assert_error("RESOURCE_QUOTA_EXCEEDED", actions.export, context, oversized)
    with patch.object(
        actions.BlobClient,
        "from_blob_url",
        side_effect=ServiceResponseError("synthetic-secret"),
    ):
        assert_error("RECONCILIATION_REQUIRED", actions.export, context, inputs)
    with TemporaryDirectory() as temp:
        path = Path(temp) / "oversized"
        path.write_bytes(b"x" * (actions.MAX_BYTES + 1))
        with patch.object(actions, "Path", return_value=path):
            assert_error(
                "RESOURCE_QUOTA_EXCEEDED",
                actions.collect,
                context,
                actions.CollectInput(fixture_id="diagnostic-sample-01", max_records=1),
            )
