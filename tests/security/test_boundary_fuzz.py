"""Property tests for untrusted YAML and signed-plan bytes."""

import asyncio
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from remote_intent import IntentError
from remote_intent.contracts import canonical
from remote_intent.parser import parse_yaml
from remote_intent.security import ExecutionPlan
from tests.agent_sim.test_protocol import document, fixture
from tests.security.test_mcp_penetration import assert_no_admission


@pytest.fixture(scope="module")
def wire_fixture(tmp_path_factory):
    path = tmp_path_factory.mktemp("wire-fuzz") / "endpoint.sqlite"
    _, controller, endpoint, calls = fixture(path, 93)
    actions = controller.catalog("fixture-user", "fake-endpoint")["actions"]
    plan = controller.compile("fixture-user", json.dumps(document(*actions)))
    now = int(controller.clock())
    signed = controller.signer.sign(
        ExecutionPlan(
            execution_id="execution",
            nonce="nonce",
            principal="fixture-user",
            issued_at=now,
            expires_at=now + 60,
            policy_revision="fixture-v1",
            resources=[],
            plan=plan,
        )
    )
    with asyncio.Runner() as runner:
        yield runner, endpoint, calls, signed


@given(st.text(max_size=2048))
def test_arbitrary_yaml_returns_canonical_data_or_public_rejection(source):
    try:
        result = parse_yaml(source)
    except IntentError as error:
        assert error.code in {
            "YAML_PARSE_FAILED",
            "YAML_UNSAFE_CONSTRUCT",
            "YAML_RESOURCE_LIMIT",
            "WORKFLOW_SCHEMA_INVALID",
        }
    else:
        assert json.loads(canonical(result)) == result


@given(st.binary(max_size=2048))
def test_arbitrary_wire_bytes_cannot_admit_execution(wire_fixture, raw):
    runner, endpoint, calls, _ = wire_fixture
    with pytest.raises(IntentError) as error:
        runner.run(endpoint.deliver(raw))
    assert error.value.code == "PLAN_SIGNATURE_INVALID"
    assert_no_admission(endpoint, calls)


@given(
    st.sampled_from(
        ["principal", "nonce", "execution_id", "approval", "policy_revision"]
    ),
    st.from_regex(r"[a-z][a-z0-9]{0,24}", fullmatch=True),
)
def test_modified_signed_claim_never_admits_execution(wire_fixture, claim, value):
    runner, endpoint, calls, signed = wire_fixture
    envelope = json.loads(signed)
    envelope["payload"][claim] = value + "-altered"
    with pytest.raises(IntentError) as error:
        runner.run(endpoint.deliver(canonical(envelope)))
    assert error.value.code == "PLAN_SIGNATURE_INVALID"
    assert_no_admission(endpoint, calls)


@given(st.integers(min_value=0x110000, max_value=0xFFFFFFFF))
def test_out_of_range_yaml_escape_is_a_public_parse_error(codepoint):
    with pytest.raises(IntentError) as error:
        parse_yaml(f'field: "\\U{codepoint:08x}"')
    assert error.value.code == "YAML_PARSE_FAILED"
