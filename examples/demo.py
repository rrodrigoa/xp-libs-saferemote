"""Run two registered example actions through a signed fake endpoint."""

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from remote_intent import ActionRegistry, EndpointRuntime, WorkflowCompiler
from remote_intent.compiler import CompiledPlan
from remote_intent.controller import Controller
from remote_intent.journal import Journal
from remote_intent.policy import Access, PolicyDecision
from remote_intent.security import Signer, TrustStore
from remote_intent.transports import FakeTransport

from . import sample_actions


class DemoPolicy:
    def evaluate(
        self,
        principal: str,
        plan: CompiledPlan,
        *,
        step_id: str | None,
        arguments: dict[str, Any] | None,
    ) -> PolicyDecision:
        access = Access.ALLOW if principal == "demo-user" else Access.DENY
        return PolicyDecision(access, "demo-v1")


async def run(path: Path) -> dict[str, Any]:
    source = Path(sample_actions.__file__)
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    registry = ActionRegistry()
    registry.install(sample_actions.collect_logs, implementation_digest=fingerprint)
    registry.install(sample_actions.summarize_logs, implementation_digest=fingerprint)
    compiler = WorkflowCompiler(registry, "demo")
    private_key = Ed25519PrivateKey.generate()
    endpoint = EndpointRuntime(
        target_id="fake-endpoint",
        compiler=compiler,
        trust=TrustStore({"demo": private_key.public_key()}),
        journal=Journal(path),
        policy=DemoPolicy(),
        result_projection=lambda _, output: output,
    )
    controller = Controller(
        target_id="fake-endpoint",
        compiler=compiler,
        signer=Signer("demo", private_key),
        transport=FakeTransport(endpoint),
        policy=DemoPolicy(),
        visible=lambda principal, target, uses: principal == "demo-user",
    )
    plan = controller.compile(
        "demo-user", source.with_name("summarize.yaml").read_text(encoding="utf-8")
    )
    execution = await controller.submit("demo-user", plan, "demo-run")
    assert execution["status"] == "succeeded", execution
    return endpoint.results(execution["execution_id"], "demo-user")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="rir-demo-") as directory:
        print(json.dumps(asyncio.run(run(Path(directory) / "demo.sqlite")), indent=2))
