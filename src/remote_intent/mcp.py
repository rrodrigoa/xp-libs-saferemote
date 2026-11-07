"""Optional MCP facade. Principal identity is supplied by the trusted host."""

from collections.abc import Awaitable, Callable
from importlib.resources import files
from pathlib import Path
from typing import Any, ParamSpec

from fastmcp import Context, FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from jsonschema import Draft202012Validator
from mcp_types import CallToolRequestParams

from .compiler import error_path
from .contracts import canonical
from .controller import Controller
from .errors import IntentError
from .errors import get_error_info as lookup_error

_P = ParamSpec("_P")
SKILL_URI = "rir://skills/remote-intent/v1"


class _StrictToolArguments(Middleware):
    def __init__(self, server: FastMCP) -> None:
        self.server = server

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        request = context.message
        tool = await self.server.get_tool(request.name)
        if tool is None:
            return ToolResult(
                structured_content=IntentError("MCP_TOOL_UNAVAILABLE").envelope()
            )
        arguments = request.arguments or {}
        schema = {**tool.parameters, "additionalProperties": False}
        error = next(Draft202012Validator(schema).iter_errors(arguments), None)
        if error is not None:
            parts = list(error.absolute_path)
            if error.validator == "required":
                parts.append(
                    next(
                        name for name in error.validator_value if name not in arguments
                    )
                )
            elif error.validator == "additionalProperties":
                parts.append("undeclared argument")
            intent = IntentError(
                "MCP_TOOL_ARGUMENT_INVALID",
                tool=request.name,
                path=error_path("arguments", parts),
            )
            return ToolResult(structured_content=intent.envelope())
        return await call_next(context)


def create_server(
    controller: Controller, authenticate: Callable[[Context], Awaitable[str | None]]
) -> FastMCP:
    """Expose the nine workflow tools and the complete versioned skill."""
    server = FastMCP("Remote Intent Runtime")
    server.add_middleware(_StrictToolArguments(server))

    async def identity(ctx: Context) -> str:
        principal = await authenticate(ctx)
        if not principal:
            raise IntentError("MCP_SESSION_UNAUTHENTICATED")
        return principal

    async def respond(
        operation: Callable[_P, Awaitable[Any]], *args: _P.args, **kwargs: _P.kwargs
    ) -> dict[str, Any]:
        try:
            envelope = {"status": "ok", "data": await operation(*args, **kwargs)}
            if len(canonical(envelope)) > 131072:
                raise IntentError("MCP_RESULT_LIMIT")
            return envelope
        except IntentError as exc:
            return exc.envelope()
        except Exception:
            return IntentError("INTERNAL_ERROR").envelope()

    @server.resource(SKILL_URI, mime_type="text/markdown")
    def skill() -> str:
        resource = files("remote_intent").joinpath("SKILL.md")
        if resource.is_file():
            return resource.read_text(encoding="utf-8")
        editable = (
            Path(__file__).resolve().parents[2]
            / "skills"
            / "remote-intent"
            / "SKILL.md"
        )
        if editable.is_file():
            return editable.read_text(encoding="utf-8")
        raise IntentError("SKILL_RESOURCE_UNAVAILABLE")

    @server.tool
    async def list_actions(target_id: str, ctx: Context) -> dict[str, Any]:
        """List currently visible registered action keys for this target."""

        async def operation() -> Any:
            catalog = controller.catalog(await identity(ctx), target_id)
            fields = ("uses", "version", "description", "effect")
            catalog["actions"] = [
                {key: spec[key] for key in fields} for spec in catalog["actions"]
            ]
            return catalog

        return await respond(operation)

    @server.tool
    async def describe_action(
        target_id: str, name: str, ctx: Context
    ) -> dict[str, Any]:
        """Read the current action contract and its catalog epoch."""

        async def operation() -> Any:
            catalog = controller.catalog(await identity(ctx), target_id)
            for action in catalog["actions"]:
                if action["uses"] == name:
                    return {**action, "catalog_epoch": catalog["catalog_epoch"]}
            raise IntentError("ACTION_UNKNOWN", action=name)

        return await respond(operation)

    @server.tool
    async def validate_workflow(
        workflow_yaml: str, ctx: Context, catalog_epoch: str | None = None
    ) -> dict[str, Any]:
        """Validate grammar, action inputs and dependencies without dispatch."""

        async def operation() -> Any:
            plan = controller.compile(await identity(ctx), workflow_yaml, catalog_epoch)
            return {
                "dependencies": plan.dependencies,
                "catalog_epoch": plan.catalog_epoch,
                "plan_digest": plan.fingerprint,
            }

        return await respond(operation)

    @server.tool
    async def preview_execution(
        workflow_yaml: str, ctx: Context, catalog_epoch: str | None = None
    ) -> dict[str, Any]:
        """Preview policy and resolved effects without dispatch."""

        async def operation() -> Any:
            principal = await identity(ctx)
            return controller.preview(
                principal, controller.compile(principal, workflow_yaml, catalog_epoch)
            )

        return await respond(operation)

    @server.tool
    async def submit_workflow(
        workflow_yaml: str,
        idempotency_key: str,
        ctx: Context,
        catalog_epoch: str | None = None,
    ) -> dict[str, Any]:
        """Request an authorized workflow execution."""

        async def operation() -> Any:
            principal = await identity(ctx)
            plan = controller.compile(principal, workflow_yaml, catalog_epoch)
            return await controller.submit(principal, plan, idempotency_key)

        return await respond(operation)

    @server.tool
    async def get_execution_status(execution_id: str, ctx: Context) -> dict[str, Any]:
        """Read durable workflow and step states for this principal."""

        async def operation() -> Any:
            return await controller.transport.status(execution_id, await identity(ctx))

        return await respond(operation)

    @server.tool
    async def get_execution_results(execution_id: str, ctx: Context) -> dict[str, Any]:
        """Read authorized projected outputs for this principal."""

        async def operation() -> Any:
            return await controller.transport.results(execution_id, await identity(ctx))

        return await respond(operation)

    @server.tool
    async def cancel_execution(execution_id: str, ctx: Context) -> dict[str, Any]:
        """Request cancellation of an active execution."""

        async def operation() -> Any:
            return {
                "requested": await controller.transport.request_cancel(
                    execution_id, await identity(ctx)
                )
            }

        return await respond(operation)

    @server.tool
    async def get_error_info(code: str, ctx: Context) -> dict[str, Any]:
        """Read the canonical public error record."""

        async def operation() -> Any:
            await identity(ctx)
            return {"code": code, **lookup_error(code)}

        return await respond(operation)

    return server
