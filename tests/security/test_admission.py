import asyncio
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from remote_intent import IntentError
from remote_intent.contracts import canonical
from remote_intent.errors import get_error_info
from remote_intent.policy import Access, DenyAllPolicy
from remote_intent.security import ExecutionPlan, Signer
from tests.agent_sim.test_protocol import document, fixture


@pytest.mark.parametrize(
    ("variant", "code"),
    [
        ("tampered", "PLAN_SIGNATURE_INVALID"),
        ("noncanonical", "PLAN_SIGNATURE_INVALID"),
        ("duplicate", "PLAN_SIGNATURE_INVALID"),
        ("untrusted", "TRUST_STORE_MISMATCH"),
        ("expired", "PLAN_EXPIRED"),
        ("target", "TARGET_MISMATCH"),
        ("revision", "POLICY_STALE"),
        ("drift", "ACTION_SCHEMA_MISMATCH"),
        ("denied", "SECURITY_DENIED"),
        ("approval", "APPROVAL_REQUIRED"),
    ],
)
def test_endpoint_rejects_before_effect(tmp_path, variant, code):
    _, controller, endpoint, calls = fixture(tmp_path / "run.sqlite", 22)
    catalog = controller.catalog("fixture-user", "fake-endpoint")
    plan = controller.compile("fixture-user", json.dumps(document(*catalog["actions"])))
    now = int(controller.clock())
    payload = ExecutionPlan(
        execution_id="test",
        nonce="unique",
        principal="fixture-user",
        issued_at=now,
        expires_at=now + 60,
        policy_revision="fixture-v1",
        resources=[],
        plan=plan,
    )
    signer = controller.signer
    if variant == "untrusted":
        signer = Signer("unknown", Ed25519PrivateKey.generate())
    elif variant == "expired":
        payload.issued_at, payload.expires_at = now - 60, now
    elif variant == "target":
        plan.workflow.spec.target.device_id = "other"
    elif variant == "revision":
        payload.policy_revision = "outdated"
    elif variant == "drift":
        plan.catalog_epoch = "old"
    elif variant == "denied":
        endpoint.policy = DenyAllPolicy()
    elif variant == "approval":
        endpoint.policy.access = Access.APPROVAL
    raw = signer.sign(payload)
    if variant == "tampered":
        body = json.loads(raw)
        body["payload"]["principal"] = "another-user"
        raw = canonical(body)
    elif variant == "noncanonical":
        raw += b" "
    elif variant == "duplicate":
        raw = b'{"key_id":"fixture",' + raw[1:]
    with pytest.raises(IntentError) as error:
        asyncio.run(endpoint.deliver(raw))
    assert error.value.code == code
    assert calls == []
    with endpoint.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0


def test_public_errors_redact_unknown_codes():
    private = "https://host.invalid/?secret=private-value"
    with pytest.raises(IntentError) as error:
        get_error_info(private)
    assert private not in json.dumps(error.value.envelope())


def test_approval_binds_plan_and_rechecks_at_endpoint(tmp_path):
    _, controller, endpoint, calls = fixture(
        tmp_path / "approval.sqlite", 25, Access.APPROVAL
    )

    class Provider:
        issued = ""

        def verify(self, receipt, binding, now):
            return receipt == "fixture-receipt" and self.issued == binding

    provider = Provider()
    controller.approvals = endpoint.approvals = provider

    def issue(binding):
        provider.issued = binding
        return "fixture-receipt"

    controller.approval_receipt = issue
    actions = controller.catalog("fixture-user", "fake-endpoint")["actions"]
    plan = controller.compile("fixture-user", json.dumps(document(*actions)))

    async def run():
        status = await controller.submit("fixture-user", plan, "approved")
        assert status["status"] == "succeeded"
        altered = controller.compile(
            "fixture-user", json.dumps(document(*actions, arg=8))
        )
        with pytest.raises(IntentError) as error:
            await controller.submit("fixture-user", altered, "approved")
        assert error.value.code == "PLAN_REPLAY_CONFLICT"
        provider.issued = "revoked"
        with pytest.raises(IntentError) as error:
            await controller.submit("fixture-user", plan, "approved")
        assert error.value.code == "APPROVAL_INVALID"

    asyncio.run(run())
    assert calls == ["measure", "summarize"]


def test_controller_blocks_hidden_actions_wrong_targets_and_stale_submission(
    tmp_path, monkeypatch
):
    from remote_intent.policy import PolicyDecision

    _, controller, _, calls = fixture(
        tmp_path / "controller.sqlite", 28, Access.APPROVAL
    )
    actions = controller.catalog("fixture-user", "fake-endpoint")["actions"]
    source = document(*actions)
    yaml = json.dumps(source)

    with pytest.raises(IntentError) as error:
        controller.catalog("fixture-user", "unknown")
    assert error.value.code == "SECURITY_DENIED"

    source["spec"]["target"]["device_id"] = "other"
    with pytest.raises(IntentError) as error:
        controller.compile("fixture-user", json.dumps(source))
    assert error.value.code == "TARGET_MISMATCH"

    visible = controller.visible
    controller.visible = lambda user, target, uses: uses != actions[0]["uses"]
    with pytest.raises(IntentError) as error:
        controller.compile("fixture-user", yaml)
    assert error.value.code == "ACTION_UNKNOWN"
    controller.visible = visible
    compile_yaml = controller.compiler.compile

    def stale(source):
        result = compile_yaml(source)
        result.catalog_epoch = "rotated"
        return result

    with monkeypatch.context() as patch:
        patch.setattr(controller.compiler, "compile", stale)
        with pytest.raises(IntentError) as error:
            controller.compile("fixture-user", yaml)
        assert error.value.code == "CATALOG_STALE"

    plan = controller.compile("fixture-user", yaml)

    class Revised:
        def evaluate(self, principal, plan, *, step_id, arguments):
            return PolicyDecision(Access.APPROVAL, "revised")

    async def check():
        for key, code in (
            ("", "MCP_TOOL_ARGUMENT_INVALID"),
            ("pending", "APPROVAL_REQUIRED"),
        ):
            with pytest.raises(IntentError) as error:
                await controller.submit("fixture-user", plan, key)
            assert error.value.code == code
        controller.policy = Revised()
        with pytest.raises(IntentError) as error:
            await controller.submit("fixture-user", plan, "pending")
        assert error.value.code == "POLICY_STALE"
        slot = controller._submissions["fixture-user", "pending"]
        controller._submissions.update(
            {("fixture-user", str(index)): slot for index in range(4096)}
        )
        with pytest.raises(IntentError) as error:
            await controller.submit("fixture-user", plan, "next")
        assert error.value.code == "RESOURCE_QUOTA_EXCEEDED"

    asyncio.run(check())
    assert calls == []


def test_signed_envelope_size_gate(tmp_path):
    _, _, endpoint, calls = fixture(tmp_path / "size.sqlite", 69)
    with pytest.raises(IntentError) as error:
        asyncio.run(endpoint.deliver(b"x" * 1_048_577))
    assert error.value.code == "RESOURCE_QUOTA_EXCEEDED"
    assert calls == []
