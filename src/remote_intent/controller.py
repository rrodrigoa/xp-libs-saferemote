"""Trusted controller boundary for discovery, admission and dispatch."""

import asyncio
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .compiler import CompiledPlan, WorkflowCompiler
from .contracts import digest
from .errors import IntentError
from .policy import (
    Access,
    ApprovalProvider,
    DenyAllPolicy,
    PolicyEvaluator,
    approval_binding,
    check_approval,
    evaluate,
)
from .security import ExecutionPlan, Signer
from .transports import TransportAdapter


class Controller:
    """A controller for one trusted inventory target."""

    def __init__(
        self,
        *,
        target_id: str,
        compiler: WorkflowCompiler,
        signer: Signer,
        transport: TransportAdapter,
        visible: Callable[[str, str, str], bool],
        policy: PolicyEvaluator | None = None,
        approvals: ApprovalProvider | None = None,
        approval_receipt: Callable[[str], str | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.target_id = target_id
        self.compiler = compiler
        self.signer = signer
        self.transport = transport
        self.visible = visible
        self.policy = policy if policy is not None else DenyAllPolicy()
        self.approvals = approvals
        self.approval_receipt = approval_receipt
        self.clock = clock
        self._submissions: dict[tuple[str, str], tuple[str, ExecutionPlan]] = {}
        self._lock = asyncio.Lock()

    def catalog(self, principal: str, target: str) -> dict[str, Any]:
        if target != self.target_id or not self.visible(principal, target, ""):
            raise IntentError("SECURITY_DENIED")
        all_specs = [
            spec.model_dump(mode="json")
            for spec in self.compiler.registry.catalog(self.compiler.platform)
        ]
        return {
            "catalog_epoch": digest(all_specs),
            "actions": [
                spec
                for spec in all_specs
                if self.visible(principal, target, spec["uses"])
            ],
        }

    def compile(
        self, principal: str, yaml: str, epoch: str | None = None
    ) -> CompiledPlan:
        catalog = self.catalog(principal, self.target_id)
        if epoch is not None and epoch != catalog["catalog_epoch"]:
            raise IntentError("CATALOG_STALE")
        plan = self.compiler.compile(yaml)
        if plan.workflow.spec.target.device_id != self.target_id:
            raise IntentError("TARGET_MISMATCH")
        if plan.catalog_epoch != catalog["catalog_epoch"]:
            raise IntentError("CATALOG_STALE")
        allowed = {action["uses"] for action in catalog["actions"]}
        if any(spec.uses not in allowed for spec in plan.actions.values()):
            raise IntentError("ACTION_UNKNOWN")
        return plan

    def preview(self, principal: str, plan: CompiledPlan) -> dict[str, Any]:
        decision = evaluate(self.policy, principal, plan)
        return {
            "catalog_epoch": plan.catalog_epoch,
            "plan_digest": plan.fingerprint,
            "authorization": decision.access,
            "dependencies": plan.dependencies,
            "steps": {
                step: {
                    "uses": spec.uses,
                    "version": spec.version,
                    "effect": spec.effect,
                    "privilege": spec.privilege,
                }
                for step, spec in plan.actions.items()
            },
        }

    async def submit(
        self, principal: str, plan: CompiledPlan, idempotency_key: str
    ) -> dict[str, Any]:
        if not 1 <= len(idempotency_key) <= 128:
            raise IntentError(
                "MCP_TOOL_ARGUMENT_INVALID",
                tool="submit_workflow",
                path="idempotency_key",
            )
        decision = evaluate(self.policy, principal, plan)
        now = int(self.clock())
        key = (principal, idempotency_key)
        async with self._lock:
            previous = self._submissions.get(key)
            if previous is not None:
                if previous[0] != plan.fingerprint:
                    raise IntentError("PLAN_REPLAY_CONFLICT")
                payload = previous[1]
                if (
                    payload.policy_revision != decision.revision
                    or payload.resources != sorted(decision.resources)
                ):
                    raise IntentError("POLICY_STALE")
            else:
                if len(self._submissions) >= 4096:
                    raise IntentError(
                        "RESOURCE_QUOTA_EXCEEDED", resource="controller submissions"
                    )
                payload = ExecutionPlan(
                    execution_id=uuid4().hex,
                    nonce=uuid4().hex,
                    principal=principal,
                    issued_at=now,
                    expires_at=now + 900,
                    policy_revision=decision.revision,
                    resources=sorted(decision.resources),
                    plan=plan.model_copy(deep=True),
                )
                self._submissions[key] = (plan.fingerprint, payload)
            if decision.access == Access.APPROVAL:
                binding = approval_binding(
                    principal, plan, decision, payload.execution_id, payload.expires_at
                )
                receipt = payload.approval
                if receipt is None and self.approval_receipt is not None:
                    receipt = self.approval_receipt(binding)
                check_approval(self.approvals, receipt, binding, now)
                payload = payload.model_copy(update={"approval": receipt})
                self._submissions[key] = (plan.fingerprint, payload)
            signed = self.signer.sign(payload)
        return await self.transport.deliver_plan(self.target_id, signed)
