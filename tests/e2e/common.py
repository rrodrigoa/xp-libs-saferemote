"""Configuration and policy shared by independent fixture processes."""

from pathlib import Path

from examples.action_packs.evidence_export.actions import (
    COLLECT,
    DESTINATION,
    EXPORT,
    MAX_BYTES,
)
from remote_intent.compiler import CompiledPlan
from remote_intent.policy import Access, PolicyDecision

TARGET = "e2e-endpoint-01"
ACCOUNT = "saferemotefixture"
CONTAINER = "e2e-evidence"
STORAGE_URL = f"https://azurite:10000/{ACCOUNT}"
SECRETS = Path("/secrets")
CA_FILE = str(SECRETS / "ca.pem")
PRINCIPALS = ("owner", "outsider", "approver")


class FixturePolicy:
    """Permit only bounded synthetic evidence and the exact export data path."""

    def evaluate(self, principal, plan: CompiledPlan, *, step_id, arguments):
        steps = plan.workflow.spec.steps
        exports = [step for step in steps if plan.actions[step.id].uses == EXPORT]
        access = Access.DENY
        valid = (
            principal in PRINCIPALS
            and plan.workflow.spec.target.device_id == TARGET
            and all(spec.uses in {COLLECT, EXPORT} for spec in plan.actions.values())
        )
        if valid and not exports:
            access = Access.ALLOW
        if valid and len(exports) == 1 and len(steps) == 2 and principal != "outsider":
            export = exports[0]
            collect = next(step for step in steps if step.id != export.id)
            if (
                plan.actions[collect.id].uses == COLLECT
                and collect.arguments["max_records"] <= 100
                and export.arguments
                == {
                    "source": {"ref": f"steps.{collect.id}.outputs.artifact"},
                    "destination": DESTINATION,
                }
                and (step_id != export.id or arguments["source"]["size"] <= MAX_BYTES)
            ):
                access = Access.APPROVAL if principal == "approver" else Access.ALLOW
        return PolicyDecision(access, "fixture-v1", (DESTINATION,) if exports else ())


def blob_name(execution_id: str, step_id: str) -> str:
    return f"runs/{execution_id}/{step_id}.jsonl"
