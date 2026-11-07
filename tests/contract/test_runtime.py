import asyncio
import json
import threading
from dataclasses import replace

import pytest

from remote_intent import IntentError
from remote_intent.actions import ActionContext, ActionRegistry, Effect
from remote_intent.artifacts import ArtifactScope
from remote_intent.contracts import Contract
from remote_intent.policy import Access, check_approval
from remote_intent.runtime import InProcessReadOnlyWorker
from tests.agent_sim.test_protocol import document, fixture


class Args(Contract):
    n: int


class Output(Contract):
    value: int


def echo(context, args):
    return Output(value=args.n)


def register(registry, fn=echo, **options):
    metadata = dict(
        key="demo.probe",
        version="1.0.0",
        implementation_digest="b" * 64,
        description="A test observation",
        effect=Effect.OBSERVE,
        input_model=Args,
        output_model=Output,
        platforms=("fake",),
    )
    return registry.register(fn, **{**metadata, **options})


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"key": "no_namespace"}, "ACTION_VERSION_UNAVAILABLE"),
        ({"version": "1.0"}, "ACTION_VERSION_UNAVAILABLE"),
        ({"input_model": object}, "ACTION_SCHEMA_MISMATCH"),
    ],
)
def test_malformed_registration(patch, code):
    with pytest.raises(IntentError) as err:
        register(ActionRegistry(), **patch)
    assert err.value.code == code


def test_registry_rejects_collisions_and_platform_mismatch():
    registry = ActionRegistry()
    registered = register(registry)
    assert registry.resolve("demo.probe@1", "fake") is registered
    assert registry.catalog("other") == []
    with pytest.raises(IntentError, match="not supported"):
        registry.resolve("demo.probe@1", "other")
    with pytest.raises(IntentError) as err:
        register(registry)
    assert err.value.code == "ACTION_KEY_COLLISION"


def test_artifact_scope_quota_expiry_and_integrity(monkeypatch):
    scope = ArtifactScope(quota=4, ttl=1)
    monkeypatch.setattr("remote_intent.artifacts.time.monotonic", lambda: 0)
    artifact = scope.put(b"data")
    assert scope.read(artifact) == b"data"
    for foreign in (ArtifactScope(), scope):
        tampered = artifact.model_copy(update={"sha256": "0" * 64})
        with pytest.raises(IntentError) as error:
            foreign.read(tampered)
        assert error.value.code == "ARTIFACT_SCOPE_DENIED"
    with pytest.raises(IntentError) as error:
        scope.put(b"x")
    assert error.value.code == "RESOURCE_QUOTA_EXCEEDED"
    monkeypatch.setattr("remote_intent.artifacts.time.monotonic", lambda: 2)
    with pytest.raises(IntentError) as error:
        scope.read(artifact)
    assert error.value.code == "ARTIFACT_EXPIRED"


@pytest.mark.parametrize(
    ("gate", "error"),
    [
        ("policy-offline", "POLICY_UNAVAILABLE"),
        ("denied-step", "SECURITY_DENIED"),
        ("policy-quota", "RESOURCE_QUOTA_EXCEEDED"),
        ("privilege", "SECURITY_DENIED"),
    ],
)
def test_effect_gates(tmp_path, gate, error):
    _, controller, endpoint, calls = fixture(tmp_path / "effects.sqlite", 12)
    if gate == "privilege":
        register(controller.compiler.registry, privilege="administrator")
    action_set = controller.catalog("fixture-user", "fake-endpoint")["actions"]
    source = document(*action_set[:2])
    if gate == "privilege":
        source["spec"]["steps"] = [
            {"id": "measure", "uses": "demo.probe@1", "with": {"n": 7}}
        ]
        source["spec"]["outputs"] = {}

    class Policy:
        def evaluate(self, principal, plan, *, step_id, arguments):
            if gate == "policy-offline":
                raise RuntimeError("secret machine information")
            base = controller.policy.evaluate(
                principal, plan, step_id=step_id, arguments=arguments
            )
            if gate == "policy-quota":
                return replace(base, timeout_seconds=30)
            if gate == "denied-step" and step_id is not None:
                return replace(base, access=Access.DENY)
            return base

    endpoint.policy = Policy()
    plan = controller.compile("fixture-user", json.dumps(source))
    if gate == "denied-step":
        state = asyncio.run(controller.submit("fixture-user", plan, "effects"))
        assert state["steps"]["measure"]["code"] == error
    else:
        with pytest.raises(IntentError) as err:
            asyncio.run(controller.submit("fixture-user", plan, "effects"))
        assert err.value.code == error
        assert "secret" not in str(err.value)
    assert calls == []


@pytest.mark.parametrize(
    ("receipt", "accepted"), [(None, False), ("forged", False), ("approved", True)]
)
def test_approval_verification(receipt, accepted):
    class Provider:
        def verify(self, token, binding, now):
            return token == "approved" and binding == "exact" and now < 42

    if accepted:
        check_approval(Provider(), receipt, "exact", 41)
        with pytest.raises(IntentError, match="invalid"):
            check_approval(Provider(), receipt, "different", 41)
    else:
        with pytest.raises(IntentError):
            check_approval(Provider(), receipt, "exact", 41)


def test_worker_signals_cooperative_timeout():
    completed = threading.Event()

    def waiting(context, args):
        assert context.cancellation.wait(3)
        completed.set()
        return Output(value=args.n)

    async def run():
        action = register(ActionRegistry(), waiting)
        worker = InProcessReadOnlyWorker()
        context = ActionContext("job", "measure", "target", threading.Event())
        with pytest.raises(IntentError) as err:
            await worker.invoke(action, context, Args(n=1), timeout=0.01)
        assert err.value.code == "STEP_TIMEOUT"
        assert completed.is_set()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("wrong-model", "ACTION_OUTPUT_INVALID"),
        ("invalid-output", "ACTION_OUTPUT_INVALID"),
        ("oversize", "RESOURCE_QUOTA_EXCEEDED"),
        ("exception", "STEP_EXECUTION_FAILED"),
    ],
)
def test_invalid_worker_outputs_are_withheld(tmp_path, mode, expected):
    _, controller, endpoint, _ = fixture(tmp_path / "result.sqlite", 60)

    class LargeOutput(Contract):
        blob: str

    class Worker:
        async def invoke(self, action, context, arguments, timeout):
            if mode == "exception":
                raise ValueError("private value from device")
            if mode == "wrong-model":
                return Output(value=1)
            return action.output_model.model_construct(
                blob=1 if mode == "invalid-output" else "x" * 66000
            )

    registry = controller.compiler.registry
    register(registry, output_model=LargeOutput)
    endpoint.worker = Worker()
    endpoint.result_projection = None
    plan = controller.compile(
        "fixture-user",
        json.dumps(
            {
                "apiVersion": "remote.intent/v1",
                "kind": "Workflow",
                "metadata": {"name": "outputs"},
                "spec": {
                    "target": {"device_id": "fake-endpoint"},
                    "steps": [
                        {"id": "read", "uses": "demo.probe@1", "with": {"n": 1}},
                        {
                            "id": "follow",
                            "uses": "demo.probe@1",
                            "with": {"n": 1},
                            "depends_on": ["read"],
                        },
                    ],
                    "outputs": {"result": {"ref": "steps.read.outputs.blob"}},
                },
            }
        ),
    )
    status = asyncio.run(controller.submit("fixture-user", plan, "invalid-output"))
    assert status["steps"]["read"]["code"] == expected
    assert status["steps"]["follow"]["status"] == "skipped"
    assert endpoint.results(status["execution_id"], "fixture-user") == {}
    assert "private value" not in json.dumps(status)


def test_cancel_signals_running_worker_and_persists_terminal_state(tmp_path):
    _, controller, endpoint, calls = fixture(tmp_path / "cancel.sqlite", 61)

    async def run():
        running = asyncio.Event()
        current = {}

        class Worker:
            async def invoke(self, action, context, arguments, timeout):
                current["context"] = context
                running.set()
                await asyncio.to_thread(context.cancellation.wait, 3)
                assert context.cancellation.is_set()
                return action.output_model(output_61=7)

        endpoint.worker = Worker()
        actions = controller.catalog("fixture-user", "fake-endpoint")["actions"]
        plan = controller.compile("fixture-user", json.dumps(document(*actions)))
        submission = asyncio.create_task(
            controller.submit("fixture-user", plan, "cancel")
        )
        await asyncio.wait_for(running.wait(), 5)
        execution_id = current["context"].execution_id
        assert await controller.transport.request_cancel(execution_id, "fixture-user")
        status = await submission
        assert status["status"] == "canceled"
        assert all(step["status"] == "canceled" for step in status["steps"].values())
        assert calls == []

    asyncio.run(run())


def test_abort_of_delivery_signals_worker_and_records_cancellation(tmp_path):
    _, controller, endpoint, _ = fixture(tmp_path / "abort.sqlite", 62)
    started = threading.Event()
    completed = threading.Event()

    def observe(context, arguments):
        started.set()
        assert context.cancellation.wait(3)
        completed.set()
        return Output(value=1)

    register(controller.compiler.registry, observe)
    plan = controller.compile(
        "fixture-user",
        json.dumps(
            {
                "apiVersion": "remote.intent/v1",
                "kind": "Workflow",
                "metadata": {"name": "cancel"},
                "spec": {
                    "target": {"device_id": "fake-endpoint"},
                    "steps": [{"id": "read", "uses": "demo.probe@1", "with": {"n": 1}}],
                },
            }
        ),
    )

    async def run():
        submission = asyncio.create_task(
            controller.submit("fixture-user", plan, "abort")
        )
        assert await asyncio.to_thread(started.wait, 3)
        submission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submission

    asyncio.run(run())
    assert completed.is_set()
    with endpoint.journal.connection() as db:
        assert db.execute("SELECT status FROM executions").fetchone()[0] == "canceled"


def test_registry_rejects_open_or_coercive_models():
    from pydantic import BaseModel, ConfigDict

    class Open(BaseModel):
        model_config = ConfigDict(strict=True)
        x: str

    class Coercive(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: str

    for model in (Open, Coercive):
        with pytest.raises(IntentError) as error:
            register(ActionRegistry(), input_model=model)
        assert error.value.code == "ACTION_SCHEMA_MISMATCH"


def test_policy_rejects_malformed_decision_and_approval_provider_outage(tmp_path):
    from remote_intent.policy import evaluate

    _, controller, _, calls = fixture(tmp_path / "policy.sqlite", 68)
    actions = controller.catalog("fixture-user", "fake-endpoint")["actions"]
    plan = controller.compile("fixture-user", json.dumps(document(*actions)))

    class InvalidPolicy:
        def evaluate(self, principal, plan, *, step_id, arguments):
            return None

    class OfflineProvider:
        def verify(self, token, binding, now):
            raise ConnectionError("provider offline")

    with pytest.raises(IntentError) as error:
        evaluate(InvalidPolicy(), "fixture-user", plan)
    assert error.value.code == "POLICY_UNAVAILABLE"
    with pytest.raises(IntentError) as error:
        check_approval(OfflineProvider(), "receipt", "binding", 42)
    assert error.value.code == "POLICY_UNAVAILABLE"
    assert calls == []


def test_workflow_output_budget_is_independent_of_each_step(tmp_path):
    _, controller, endpoint, _ = fixture(tmp_path / "workflow-limit.sqlite", 73)

    class Result(Contract):
        data: str

    def observe(context, args):
        return Result(data="x" * 40000)

    register(controller.compiler.registry, observe, output_model=Result)
    plan = controller.compile(
        "fixture-user",
        json.dumps(
            {
                "apiVersion": "remote.intent/v1",
                "kind": "Workflow",
                "metadata": {"name": "limited"},
                "spec": {
                    "target": {"device_id": "fake-endpoint"},
                    "steps": [
                        {"id": step, "uses": "demo.probe@1", "with": {"n": 1}}
                        for step in ("first", "second")
                    ],
                    "outputs": {
                        step: {"ref": f"steps.{step}.outputs.data"}
                        for step in ("first", "second")
                    },
                },
            }
        ),
    )
    with pytest.raises(IntentError) as error:
        asyncio.run(controller.submit("fixture-user", plan, "aggregate-limit"))
    assert error.value.code == "RESOURCE_QUOTA_EXCEEDED"
    with endpoint.journal.connection() as db:
        assert db.execute("SELECT status FROM executions").fetchone()[0] == "failed"


@pytest.mark.parametrize(
    ("tick", "expected"), [(3, "STEP_TIMEOUT"), (4, "PLAN_EXPIRED")]
)
def test_endpoint_checks_time_again_before_step_invocation(tmp_path, tick, expected):
    _, controller, endpoint, calls = fixture(tmp_path / "expiry.sqlite", 76)
    start = 1000
    controller.clock = lambda: start
    times = iter([start] * (tick - 1))
    endpoint.clock = lambda: next(times, start + 901)
    actions = controller.catalog("fixture-user", "fake-endpoint")["actions"]
    plan = controller.compile("fixture-user", json.dumps(document(*actions)))
    status = asyncio.run(controller.submit("fixture-user", plan, "expire"))
    assert status["steps"]["measure"]["code"] == expected
    assert calls == []
