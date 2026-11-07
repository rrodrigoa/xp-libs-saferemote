"""Fake-only MCP fixture shared by the model runner."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastmcp import Context, FastMCP
from pydantic import Field, create_model

from remote_intent import (
    ActionContext,
    ActionRegistry,
    Effect,
    EndpointRuntime,
    WorkflowCompiler,
)
from remote_intent.compiler import CompiledPlan
from remote_intent.contracts import Contract
from remote_intent.controller import Controller
from remote_intent.journal import Journal
from remote_intent.mcp import create_server
from remote_intent.policy import Access, PolicyDecision
from remote_intent.security import Signer, TrustStore
from remote_intent.transports import FakeTransport

TARGET = "fake-endpoint"


@dataclass
class FixturePolicy:
    access: Access

    def evaluate(
        self,
        principal: str,
        plan: CompiledPlan,
        *,
        step_id: str | None,
        arguments: dict[str, object] | None,
    ) -> PolicyDecision:
        return PolicyDecision(self.access, "fixture-v1")


def create_fixture(
    path: Path, seed: int, access: Access = Access.ALLOW
) -> tuple[FastMCP, list[str]]:
    """Build two seeded schema contracts and a signed loopback endpoint."""
    input_field = f"input_{seed}"
    output_field = f"output_{seed}"
    input_fields: dict[str, Any] = {input_field: (int, Field(ge=1, le=25))}
    output_fields: dict[str, Any] = {output_field: (int, Field(ge=1, le=25))}
    args = create_model("ProbeInput", __base__=Contract, **input_fields)
    results = create_model("ProbeResult", __base__=Contract, **output_fields)
    registry = ActionRegistry()
    calls: list[str] = []

    def observe(context: ActionContext, inputs: Contract) -> Contract:
        calls.append(context.step_id)
        return results(**{output_field: getattr(inputs, input_field)})

    for verb in ("inspect", "summarize"):
        registry.register(
            observe,
            key=f"fake.pack_{seed}.{verb}",
            version="1.0.0",
            input_model=args,
            output_model=results,
            description=f"Fake {verb}",
            effect=Effect.OBSERVE,
            implementation_digest="a" * 64,
        )

    compiler = WorkflowCompiler(registry, "fake")
    private_key = Ed25519PrivateKey.generate()
    policy = FixturePolicy(access)
    endpoint = EndpointRuntime(
        target_id=TARGET,
        compiler=compiler,
        trust=TrustStore({"fake": private_key.public_key()}),
        journal=Journal(path),
        policy=policy,
        result_projection=lambda _, output: output,
    )
    controller = Controller(
        target_id=TARGET,
        compiler=compiler,
        signer=Signer("fake", private_key),
        policy=policy,
        transport=FakeTransport(endpoint),
        visible=lambda principal, target, uses: principal == "fake-user",
    )

    async def authenticate(ctx: Context) -> str:
        return "fake-user"

    return create_server(controller, authenticate), calls
