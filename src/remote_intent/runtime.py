"""Independent endpoint admission and durable generic invocation."""

import asyncio
import inspect
import time
from collections.abc import Callable
from copy import deepcopy
from threading import Event
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .actions import ActionContext, Effect, RegisteredAction
from .artifacts import ArtifactScope
from .compiler import WorkflowCompiler
from .contracts import canonical
from .errors import IntentError
from .journal import Journal
from .policy import (
    Access,
    ApprovalProvider,
    DenyAllPolicy,
    PolicyEvaluator,
    approval_binding,
    check_approval,
    evaluate,
)
from .security import ExecutionPlan, TrustStore

_MISSING = object()


class Worker(Protocol):
    """Trusted isolation adapter. Invoke must quiesce work before returning or raising."""

    async def invoke(
        self,
        action: RegisteredAction,
        context: ActionContext,
        arguments: BaseModel,
        timeout: float,
    ) -> BaseModel: ...


class InProcessReadOnlyWorker:
    """Trusted test/local worker. Deadlines are cooperative for sync callables."""

    async def invoke(
        self,
        action: RegisteredAction,
        context: ActionContext,
        arguments: BaseModel,
        timeout: float,
    ) -> BaseModel:
        async def call() -> BaseModel:
            result = await asyncio.to_thread(action.invoke, context, arguments)
            return await result if inspect.isawaitable(result) else result

        task = asyncio.create_task(call())
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if done:
                return task.result()
            context.cancellation.set()
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            raise IntentError("STEP_TIMEOUT", action=action.spec.uses)
        except asyncio.CancelledError:
            context.cancellation.set()
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            raise


def _substitute(value: Any, outputs: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict) and "ref" in value:
        _, producer, _, field = value["ref"].split(".")
        return outputs.get(producer, {}).get(field, value.get("fallback", _MISSING))
    if isinstance(value, dict):
        return {key: _substitute(child, outputs) for key, child in value.items()}
    if isinstance(value, list):
        return [_substitute(child, outputs) for child in value]
    return value


def _condition(node: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> bool:
    if "all" in node:
        return all(_condition(child, outputs) for child in node["all"])
    if "any" in node:
        return any(_condition(child, outputs) for child in node["any"])
    value = _substitute(node, outputs)
    if "exists" in node:
        return bool((value is not _MISSING) == node["exists"])
    if value is _MISSING:
        return False
    choices = [node["equals"]] if "equals" in node else node["in"]
    return any(canonical(value) == canonical(choice) for choice in choices)


class EndpointRuntime:
    """Accept signed plans and recheck policy and contracts before invocation."""

    def __init__(
        self,
        *,
        target_id: str,
        compiler: WorkflowCompiler,
        trust: TrustStore,
        journal: Journal,
        policy: PolicyEvaluator | None = None,
        approvals: ApprovalProvider | None = None,
        worker: Worker | None = None,
        result_projection: Callable[[str, dict[str, Any]], dict[str, Any]]
        | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.target_id = target_id
        self.compiler = compiler
        self.trust = trust
        self.journal = journal
        self.policy = policy if policy is not None else DenyAllPolicy()
        self.approvals = approvals
        self.worker = worker if worker is not None else InProcessReadOnlyWorker()
        self.result_projection = result_projection
        self.clock = clock
        self._slots = asyncio.Semaphore(8)
        self._active: dict[str, dict[str, Event]] = {}

    def _authorize(
        self,
        payload: ExecutionPlan,
        step_id: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        now = int(self.clock())
        if now >= payload.expires_at:
            raise IntentError("PLAN_EXPIRED")
        decision = evaluate(
            self.policy,
            payload.principal,
            payload.plan,
            step_id=step_id,
            arguments=deepcopy(arguments),
        )
        if decision.revision != payload.policy_revision or sorted(
            decision.resources
        ) != sorted(payload.resources):
            raise IntentError("POLICY_STALE")
        if decision.access == Access.APPROVAL or payload.approval is not None:
            check_approval(
                self.approvals,
                payload.approval,
                approval_binding(
                    payload.principal,
                    payload.plan,
                    decision,
                    payload.execution_id,
                    payload.expires_at,
                ),
                now,
            )

    async def deliver(self, envelope: bytes) -> dict[str, Any]:
        payload, fingerprint = self.trust.verify(
            envelope, now=int(self.clock()), target_id=self.target_id
        )
        plan = payload.plan
        try:
            local = self.compiler.resolve(plan.workflow)
        except IntentError:
            raise IntentError("ACTION_SCHEMA_MISMATCH") from None
        if local.fingerprint != plan.fingerprint:
            raise IntentError("ACTION_SCHEMA_MISMATCH")
        self._authorize(payload)
        if any(
            spec.privilege is not None or spec.effect != Effect.OBSERVE
            for spec in local.actions.values()
        ):
            raise IntentError("SECURITY_DENIED")
        if self.journal.accept(
            payload.execution_id, payload.nonce, fingerprint, payload.principal
        ):
            await self._run(payload)
        return self.journal.status(payload.execution_id, payload.principal)

    async def _run(self, payload: ExecutionPlan) -> None:
        execution = payload.execution_id
        plan = payload.plan
        deadline = time.monotonic() + plan.workflow.spec.limits.timeout_seconds
        outputs: dict[str, dict[str, Any]] = {}
        artifacts = ArtifactScope()
        states: dict[str, str] = {}
        tasks: dict[str, asyncio.Task[None]] = {}
        self.journal.record(execution, "running")
        active: dict[str, Event] = {}
        self._active[execution] = active

        async def run_step(step_id: str) -> None:
            step = steps[step_id]
            spec = plan.actions[step_id]
            dependencies = plan.dependencies[step_id]
            await asyncio.gather(*(tasks[key] for key in dependencies))
            state, code = "skipped", None
            try:
                if self.journal.cancelled(execution):
                    raise IntentError("STEP_CANCELLED")
                if any(states[key] != "succeeded" for key in step.depends_on) or any(
                    states[key] not in allowed for key, allowed in step.after.items()
                ):
                    return
                if step.when is not None and not _condition(step.when, outputs):
                    return
                async with self._slots:
                    if self.journal.cancelled(execution):
                        raise IntentError("STEP_CANCELLED")
                    timeout = min(
                        spec.timeout_seconds,
                        deadline - time.monotonic(),
                        payload.expires_at - self.clock(),
                    )
                    if timeout <= 0:
                        raise IntentError("STEP_TIMEOUT", action=spec.uses)
                    registered = self.compiler.registry.resolve(
                        spec.uses, self.compiler.platform
                    )
                    if registered.spec != spec:
                        raise IntentError("ACTION_SCHEMA_MISMATCH")
                    try:
                        values = _substitute(step.arguments, outputs)
                        arguments = registered.input_model.model_validate_json(
                            canonical(values)
                        )
                    except (TypeError, ValueError):
                        raise IntentError(
                            "REFERENCE_UNAVAILABLE", path=f"steps.{step_id}.with"
                        ) from None
                    self._authorize(payload, step_id, arguments.model_dump(mode="json"))
                    self.journal.record(execution, "running", step=step_id)
                    cancellation = active[step_id] = Event()
                    context = ActionContext(
                        execution,
                        step_id,
                        self.target_id,
                        cancellation,
                        artifacts=artifacts,
                    )
                    result = await self.worker.invoke(
                        registered, context, arguments, timeout
                    )
                    if self.journal.cancelled(execution):
                        raise IntentError("STEP_CANCELLED")
                    if type(result) is not registered.output_model:
                        raise IntentError("ACTION_OUTPUT_INVALID", action=spec.uses)
                    try:
                        validated = registered.output_model.model_validate_json(
                            result.model_dump_json(warnings="error")
                        )
                        output = validated.model_dump(mode="json")
                        if len(canonical(output)) > 65536:
                            raise IntentError(
                                "RESOURCE_QUOTA_EXCEEDED", resource="inline step output"
                            )
                    except (ValidationError, ValueError):
                        raise IntentError(
                            "ACTION_OUTPUT_INVALID", action=spec.uses
                        ) from None
                    outputs[step_id] = output
                    state = "succeeded"
            except IntentError as exc:
                state = "canceled" if exc.code == "STEP_CANCELLED" else "failed"
                code = exc.code
            except asyncio.CancelledError:
                state, code = "canceled", "STEP_CANCELLED"
                raise
            except Exception:
                state, code = "failed", "STEP_EXECUTION_FAILED"
            finally:
                active.pop(step_id, None)
                states[step_id] = state
                self.journal.record(
                    execution,
                    state,
                    step=step_id,
                    code=code,
                    output=outputs.get(step_id),
                )

        steps = {step.id: step for step in plan.workflow.spec.steps}
        try:
            async with asyncio.TaskGroup() as group:
                for step_id in plan.order:
                    tasks[step_id] = group.create_task(run_step(step_id))
            state = "succeeded"
            if self.journal.cancelled(execution) or "canceled" in states.values():
                state = "canceled"
            elif "failed" in states.values():
                state = (
                    "partially_succeeded"
                    if "succeeded" in states.values()
                    else "failed"
                )
            selected = {
                name: _substitute(ref, outputs)
                for name, ref in plan.workflow.spec.outputs.items()
            }
            selected = {
                name: value for name, value in selected.items() if value is not _MISSING
            }
            if len(canonical(selected)) > 65536:
                raise IntentError(
                    "RESOURCE_QUOTA_EXCEEDED", resource="inline workflow output"
                )
            self.journal.record(execution, state, output=selected)
        except asyncio.CancelledError:
            self.journal.record(execution, "canceled", code="STEP_CANCELLED")
            raise
        except IntentError as exc:
            self.journal.record(execution, "failed", code=exc.code)
            raise
        except Exception:
            self.journal.record(execution, "failed", code="INTERNAL_ERROR")
            raise IntentError("INTERNAL_ERROR") from None
        finally:
            self._active.pop(execution, None)

    def request_cancel(self, execution_id: str, principal: str) -> bool:
        """Persist cancellation and signal the active local workers."""
        if not self.journal.cancel(execution_id, principal):
            return False
        for event in self._active.get(execution_id, {}).values():
            event.set()
        return True

    def results(self, execution_id: str, principal: str) -> dict[str, Any]:
        output = self.journal.results(execution_id, principal)
        if self.result_projection is None:
            return {}
        return self.result_projection(principal, output)
