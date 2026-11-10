"""Deterministic agent exercising real MCP and independent blob access."""

import asyncio
import base64
import hashlib
import json
import ssl
import time
from copy import deepcopy
from traceback import extract_tb
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml
from azure.core.exceptions import HttpResponseError
from azure.storage.blob import BlobClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from jsonschema import Draft202012Validator

from remote_intent.catalog import verify_catalog

from .common import CA_FILE, PRINCIPALS, SECRETS, TARGET
from .pack_contract import run_pack_contract

MCP_URL = "https://control-plane:8443/mcp"
EXPECTED_DIGEST = "3f393c26a11376e0adbf61f06a12c100300bf2dd43c597679c95d06a6ee57db8"


def client(principal=None, **headers):
    if principal in PRINCIPALS:
        headers["Authorization"] = f"Bearer {(SECRETS / principal).read_text()}"
    return Client(
        StreamableHttpTransport(
            MCP_URL,
            headers=headers,
            verify=ssl.create_default_context(cafile=CA_FILE),
        ),
        timeout=140,
    )


async def call(session, tool, **arguments):
    response = await session.call_tool(tool, arguments)
    payload = response.structured_content
    assert isinstance(payload, dict), "Expected structured MCP response"
    if tool != "get_artifact_access":
        encoded = json.dumps(payload)
        assert "sig=" not in encoded and "storage.key" not in encoded
    return payload


def denied(operation):
    try:
        operation()
    except HttpResponseError as exc:
        assert exc.status_code == 403, "Expected SAS permission rejection"
    else:
        raise AssertionError("SAS unexpectedly granted access")


def assert_no_client_certificate():
    request = Request("https://endpoint:8443/audit", data=b"{}", method="POST")
    try:
        with urlopen(
            request, context=ssl.create_default_context(cafile=CA_FILE), timeout=4
        ):
            pass
    except (URLError, OSError):
        return
    raise AssertionError("Endpoint unexpectedly accepted an unauthenticated TLS client")


async def run():
    async with client("owner") as owner:
        assert (await owner.read_resource("rir://skills/remote-intent/v1"))[0].text
        listing = (await call(owner, "list_actions", target_id=TARGET))["data"]
        keys = [item["uses"] for item in listing["actions"]]
        assert len(keys) == 2
        collect, export = [
            (await call(owner, "describe_action", target_id=TARGET, name=key))["data"]
            for key in keys
        ]
        published = (await call(owner, "fixture_catalog"))["data"]
        snapshot = verify_catalog(
            base64.b64decode(published["catalog"], validate=True),
            base64.b64decode(published["signature"], validate=True),
            publisher_key=Ed25519PublicKey.from_public_bytes(
                (SECRETS / "plan.pub").read_bytes()
            ),
            target_id=TARGET,
            now=int(time.time()),
        )
        assert snapshot.epoch == listing["catalog_epoch"]
        assert [spec.model_dump(mode="json") for spec in snapshot.actions] == [
            {key: value for key, value in spec.items() if key != "catalog_epoch"}
            for spec in (collect, export)
        ]
        assert collect["effect"] == "observe" and export["effect"] == "data_export"
        collect_args = collect["examples"][0]
        destination = export["input_schema"]["properties"]["destination"]["const"]
        Draft202012Validator(collect["input_schema"]).validate(collect_args)
        artifact = "artifact"
        assert artifact in collect["output_schema"]["properties"]
        assert {"source", "destination"} == set(export["input_schema"]["required"])
        assert {"object_ref", "content_sha256", "size_bytes", "etag"} <= set(
            export["output_schema"]["properties"]
        )
        steps = [
            {"id": "collect", "uses": collect["uses"], "with": collect_args},
            {
                "id": "export",
                "uses": export["uses"],
                "with": {
                    "source": {"ref": f"steps.collect.outputs.{artifact}"},
                    "destination": destination,
                },
            },
        ]
        workflow = {
            "apiVersion": "remote.intent/v1",
            "kind": "Workflow",
            "metadata": {"name": "investigate-and-export-evidence"},
            "spec": {
                "target": {"device_id": TARGET},
                "steps": steps,
                "outputs": {
                    "evidence": {"ref": "steps.export.outputs.object_ref"},
                    "checksum": {"ref": "steps.export.outputs.content_sha256"},
                    "size": {"ref": "steps.export.outputs.size_bytes"},
                    "etag": {"ref": "steps.export.outputs.etag"},
                },
            },
        }
        request = {
            "workflow_yaml": yaml.safe_dump(workflow),
            "catalog_epoch": listing["catalog_epoch"],
        }
        assert (await call(owner, "validate_workflow", **request))["status"] == "ok"
        preview = await call(owner, "preview_execution", **request)
        assert preview["data"]["authorization"] == "allow"
        assert preview["data"]["steps"]["export"]["effect"] == "data_export"
        baseline = (await call(owner, "fixture_audit"))["data"]
        assert baseline == {
            "executions": 0,
            "invocations": 0,
            "blobs": 0,
            "grant_scopes": 0,
        }

        for alias in ("https://169.254.169.254/data", "../../other", "unknown"):
            invalid = deepcopy(workflow)
            invalid["spec"]["steps"][1]["with"]["destination"] = alias
            result = await call(
                owner,
                "submit_workflow",
                workflow_yaml=yaml.safe_dump(invalid),
                idempotency_key="invalid",
            )
            assert result["error"]["code"] == "ACTION_INPUT_INVALID"
            assert result["error"]["path"] == "spec.steps[1].with.destination"
        for principal, expected in (
            (None, "MCP_SESSION_UNAUTHENTICATED"),
            ("outsider", "SECURITY_DENIED"),
            ("approver", "APPROVAL_REQUIRED"),
        ):
            async with client(principal, **{"X-Principal": "owner"}) as other:
                result = await call(
                    other, "submit_workflow", **request, idempotency_key="denied"
                )
                assert result["error"]["code"] == expected
        assert (await call(owner, "fixture_audit"))["data"] == baseline

        response = await call(
            owner, "submit_workflow", **request, idempotency_key="evidence"
        )
        assert response["status"] == "ok", response.get("error", {}).get("code")
        execution = response["data"]["execution_id"]
        for _ in range(20):
            status = (
                await call(owner, "get_execution_status", execution_id=execution)
            )["data"]
            if status["status"] == "succeeded":
                break
            await asyncio.sleep(0.1)
        assert status["status"] == "succeeded", status
        assert all(
            status["steps"][name]["status"] == "succeeded"
            for name in ("collect", "export")
        )
        result = (await call(owner, "get_execution_results", execution_id=execution))[
            "data"
        ]
        assert result["checksum"] == EXPECTED_DIGEST
        args = {"execution_id": execution, "object_ref": result["evidence"]}
        async with client("outsider") as outsider:
            assert (await call(outsider, "get_artifact_access", **args))[
                "status"
            ] == "error"
        assert (
            await call(
                owner,
                "get_artifact_access",
                execution_id=execution,
                object_ref="unknown",
            )
        )["status"] == "error"
        grant = await call(owner, "get_artifact_access", **args)
        assert grant["status"] == "ok", "Read delegation failed"
        from examples.action_packs.evidence_export.actions import API_VERSION

        url = grant["data"]["read_url"]
        try:
            with BlobClient.from_blob_url(
                url,
                api_version=API_VERSION,
                connection_verify=CA_FILE,
                retry_total=0,
                connection_timeout=3,
                read_timeout=5,
            ) as blob:
                download = blob.download_blob()
                data = download.readall()
                assert hashlib.sha256(data).hexdigest() == result["checksum"]
                assert len(data) == result["size"] and len(data.splitlines()) == 3
                assert download.properties.etag == result["etag"]
                denied(lambda: blob.upload_blob(b"unauthorized", overwrite=True))
                denied(blob.delete_blob)
        except HttpResponseError:
            raise RuntimeError("Direct read or permission probe failed") from None
        assert (await call(owner, "fixture_check_writer", execution_id=execution))[
            "data"
        ]["write_only"]
        replay = await call(
            owner, "submit_workflow", **request, idempotency_key="evidence"
        )
        assert replay["data"]["execution_id"] == execution
        conflict = deepcopy(workflow)
        conflict["metadata"]["name"] = "conflicting-plan"
        response = await call(
            owner,
            "submit_workflow",
            workflow_yaml=yaml.safe_dump(conflict),
            idempotency_key="evidence",
        )
        assert response["error"]["code"] == "PLAN_REPLAY_CONFLICT"
        observed = (await call(owner, "fixture_audit"))["data"]
        assert observed == {
            "executions": 1,
            "invocations": 2,
            "blobs": 1,
            "grant_scopes": 1,
        }
        for scenario, code in (
            ("signature", "PLAN_SIGNATURE_INVALID"),
            ("target", "TARGET_MISMATCH"),
            ("digest", "ACTION_SCHEMA_MISMATCH"),
            ("missing", "SECURITY_DENIED"),
            ("wrong-step", "SECURITY_DENIED"),
        ):
            denied_plan = await call(
                owner,
                "fixture_probe",
                workflow_yaml=request["workflow_yaml"],
                scenario=scenario,
            )
            assert denied_plan["error"]["code"] == code, scenario
        assert (await call(owner, "fixture_audit"))["data"] == observed
        for scenario in ("expired", "wrong-blob"):
            denied_write = await call(
                owner,
                "fixture_probe",
                workflow_yaml=request["workflow_yaml"],
                scenario=scenario,
            )
            assert (
                denied_write["data"]["steps"]["export"]["code"] == "SECURITY_DENIED"
            ), scenario
        after_denials = (await call(owner, "fixture_audit"))["data"]
        assert after_denials["blobs"] == 1 and after_denials["invocations"] == 6
        uncertain = await call(
            owner,
            "fixture_probe",
            workflow_yaml=request["workflow_yaml"],
            scenario="lost-receipt",
        )
        assert uncertain["data"]["status"] == "needs_reconciliation"
        assert uncertain["data"]["code"] == "RECONCILIATION_REQUIRED"
        repeat = await call(
            owner,
            "fixture_probe",
            workflow_yaml=request["workflow_yaml"],
            scenario="lost-receipt",
        )
        assert repeat["data"] == uncertain["data"]
        final = (await call(owner, "fixture_audit"))["data"]
        assert final == {
            "executions": 4,
            "invocations": 8,
            "blobs": 2,
            "grant_scopes": 2,
        }
        print(
            "MCP → independently verified endpoint → blob write → delegated read: passed"
        )
        print(
            f"SHA-256 {result['checksum']}; {result['size']} bytes; 2 invocations; 1 blob"
        )
        print(
            "Denied cases: 0 invocations and 0 grants; identical replay: 0 additional invocations"
        )
        print(
            "Expired and wrong-object SAS: 0 writes; lost receipt: 1 commit, no retry"
        )


if __name__ == "__main__":
    try:
        run_pack_contract()
        assert_no_client_certificate()
        asyncio.run(run())
    except Exception as exc:
        # Credential-bearing client exceptions are never formatted in runner logs.
        if isinstance(exc, AssertionError) and str(exc) in {
            "Expected structured MCP response",
            "Read delegation failed",
            "Endpoint unexpectedly accepted an unauthenticated TLS client",
        }:
            print(str(exc))
        frames = [
            frame
            for frame in extract_tb(exc.__traceback__)
            if frame.filename == __file__
        ]
        location = frames[-1].lineno if frames else 0
        print(f"Fixture failed ({type(exc).__name__}) at runner line {location}")
        raise SystemExit(1) from None
