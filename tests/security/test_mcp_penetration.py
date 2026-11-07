"""Adversarial submissions at the same MCP boundary used by an agent."""

import asyncio
import json

import pytest
from fastmcp import Client

from tests.agent_sim.test_protocol import call, document, fixture


def assert_no_admission(endpoint, calls):
    assert calls == []
    with endpoint.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("tag", "YAML_UNSAFE_CONSTRUCT"),
        ("cycle", "YAML_UNSAFE_CONSTRUCT"),
        ("merge", "YAML_UNSAFE_CONSTRUCT"),
        ("duplicate", "YAML_UNSAFE_CONSTRUCT"),
        ("document", "YAML_UNSAFE_CONSTRUCT"),
        ("depth", "YAML_RESOURCE_LIMIT"),
        ("bytes", "YAML_RESOURCE_LIMIT"),
        ("nodes", "YAML_RESOURCE_LIMIT"),
        ("principal", "WORKFLOW_SCHEMA_INVALID"),
        ("implementation", "WORKFLOW_SCHEMA_INVALID"),
        ("policy", "WORKFLOW_SCHEMA_INVALID"),
        ("approval", "WORKFLOW_SCHEMA_INVALID"),
        ("unicode", "ACTION_UNKNOWN"),
        ("command", "ACTION_UNKNOWN"),
        ("foreign-ref", "REFERENCE_UNAVAILABLE"),
        ("injection", "ACTION_INPUT_INVALID"),
    ],
)
def test_hostile_yaml_never_reaches_signer_or_endpoint(
    tmp_path, monkeypatch, payload, code
):
    server, controller, endpoint, calls = fixture(tmp_path / "penetration.sqlite", 91)
    actions = controller.catalog("fixture-user", "fake-endpoint")["actions"]
    workflow = document(*actions)
    step = workflow["spec"]["steps"][0]
    prefixes = {
        "tag": "payload: !!python/object/apply:os.system ['should-never-run']",
        "cycle": "payload: &cycle [*cycle]",
        "merge": "payload: {<<: {uses: shell.run@1}}",
        "duplicate": "payload: {uses: shell.run@1, uses: safe.inspect@1}",
        "depth": "payload: " + "[" * 25 + "]" * 25,
        "bytes": "payload: " + "a" * 65536,
        "nodes": "payload: [" + ",".join("0" for _ in range(8200)) + "]",
    }
    if payload in prefixes:
        source = prefixes[payload]
    elif payload == "document":
        source = json.dumps(workflow) + "\n---\n{}"
    else:
        if payload == "principal":
            workflow["principal"] = "administrator"
        elif payload == "implementation":
            step["implementation"] = "os:system"
        elif payload == "policy":
            workflow["spec"]["policy"] = {"access": "allow"}
        elif payload == "approval":
            workflow["spec"]["approval"] = {"receipt": "forged"}
        elif payload == "unicode":
            step["uses"] = step["uses"].replace("example", "\u0435xample")
        elif payload == "command":
            step["uses"] = "python.exec@1"
        elif payload == "foreign-ref":
            workflow["spec"]["steps"][1]["with"]["input_91"] = {
                "ref": "executions.other.steps.measure.outputs.output_91"
            }
        else:
            step["with"]["input_91"] = (
                "../../private\x00; $(id) --unsafe https://invalid.example/payload"
            )
        source = json.dumps(workflow)

    def unexpected_signing(*args, **kwargs):
        pytest.fail("Hostile YAML reached the signer")

    monkeypatch.setattr(controller.signer, "sign", unexpected_signing)

    async def submit():
        async with Client(server) as client:
            result = await call(
                client,
                "submit_workflow",
                workflow_yaml=source,
                idempotency_key="attack",
            )
            assert result["status"] == "error"
            assert result["error"]["code"] == code

    asyncio.run(submit())
    assert_no_admission(endpoint, calls)


def test_registered_schema_cannot_fetch_external_references(tmp_path, monkeypatch):
    from pydantic import Field

    from remote_intent import Effect
    from remote_intent.contracts import Contract

    class ExternalReference(Contract):
        item: int = Field(json_schema_extra={"$ref": "https://invalid.example/schema"})

    server, controller, endpoint, calls = fixture(tmp_path / "schema.sqlite", 94)
    controller.compiler.registry.register(
        lambda *_: pytest.fail("Unresolved action must never execute"),
        key="test.external",
        version="1.0.0",
        description="Fixture",
        effect=Effect.OBSERVE,
        implementation_digest="a" * 64,
        input_model=ExternalReference,
        output_model=ExternalReference,
    )

    def deny_network(*args, **kwargs):
        pytest.fail("Schema validation attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", deny_network)
    source = {
        "apiVersion": "remote.intent/v1",
        "kind": "Workflow",
        "metadata": {"name": "external"},
        "spec": {
            "target": {"device_id": "fake-endpoint"},
            "steps": [{"id": "test", "uses": "test.external@1", "with": {"item": 1}}],
        },
    }

    async def submit():
        async with Client(server) as client:
            result = await call(
                client,
                "submit_workflow",
                workflow_yaml=json.dumps(source),
                idempotency_key="external",
            )
            assert result["error"]["code"] == "ACTION_SCHEMA_MISMATCH"

    asyncio.run(submit())
    assert_no_admission(endpoint, calls)


def test_forged_principal_cannot_read_or_cancel_another_execution(tmp_path):
    from remote_intent.mcp import create_server

    _, controller, endpoint, calls = fixture(tmp_path / "ownership.sqlite", 95)
    endpoint.journal.accept("victim", "victim-nonce", "fingerprint", "victim-user")
    endpoint.journal.record("victim", "running")

    async def authenticate(ctx):
        return "different-user"

    async def attack():
        async with Client(create_server(controller, authenticate)) as client:
            for tool in (
                "get_execution_status",
                "get_execution_results",
                "cancel_execution",
            ):
                response = await call(client, tool, execution_id="victim")
                assert response["error"]["code"] == "SECURITY_DENIED"

    asyncio.run(attack())
    assert calls == []
    assert endpoint.journal.status("victim", "victim-user")["status"] == "running"
    assert not endpoint.journal.cancelled("victim")
