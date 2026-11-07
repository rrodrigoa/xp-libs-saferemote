"""Trusted authorization and exact approval binding."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .compiler import CompiledPlan
from .contracts import digest
from .errors import IntentError


class Access(StrEnum):
    DENY = "deny"
    ALLOW = "allow"
    APPROVAL = "requires_approval"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    access: Access
    revision: str
    resources: tuple[str, ...] = ()
    timeout_seconds: int = 120


class PolicyEvaluator(Protocol):
    """Service and endpoint policy independently implement this contract."""

    def evaluate(
        self,
        principal: str,
        plan: CompiledPlan,
        *,
        step_id: str | None,
        arguments: dict[str, Any] | None,
    ) -> PolicyDecision: ...


class DenyAllPolicy:
    def evaluate(
        self,
        principal: str,
        plan: CompiledPlan,
        *,
        step_id: str | None,
        arguments: dict[str, Any] | None,
    ) -> PolicyDecision:
        return PolicyDecision(Access.DENY, "default-deny")


class ApprovalProvider(Protocol):
    """Validate a receipt in the trusted approval system, including revocation."""

    def verify(self, receipt: str, binding: str, now: int) -> bool: ...


def evaluate(
    policy: PolicyEvaluator,
    principal: str,
    plan: CompiledPlan,
    *,
    step_id: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> PolicyDecision:
    try:
        decision = policy.evaluate(
            principal, plan.model_copy(deep=True), step_id=step_id, arguments=arguments
        )
        if (
            not isinstance(decision, PolicyDecision)
            or not decision.revision
            or not isinstance(decision.access, Access)
        ):
            raise ValueError
    except Exception:
        raise IntentError("POLICY_UNAVAILABLE") from None
    if not principal or decision.access == Access.DENY:
        raise IntentError("SECURITY_DENIED")
    if not 0 < plan.workflow.spec.limits.timeout_seconds <= decision.timeout_seconds:
        raise IntentError("RESOURCE_QUOTA_EXCEEDED", resource="workflow timeout")
    return decision


def approval_binding(
    principal: str,
    plan: CompiledPlan,
    decision: PolicyDecision,
    execution_id: str,
    expires: int,
) -> str:
    return digest(
        {
            "principal": principal,
            "plan": plan.fingerprint,
            "revision": decision.revision,
            "resources": sorted(decision.resources),
            "execution": execution_id,
            "expires": expires,
        }
    )


def check_approval(
    provider: ApprovalProvider | None, receipt: str | None, binding: str, now: int
) -> None:
    if receipt is None:
        raise IntentError("APPROVAL_REQUIRED")
    try:
        valid = provider is not None and provider.verify(receipt, binding, now) is True
    except Exception:
        raise IntentError("POLICY_UNAVAILABLE") from None
    if not valid:
        raise IntentError("APPROVAL_INVALID")
