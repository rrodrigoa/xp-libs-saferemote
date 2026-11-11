"""Typed delivery and execution-query interfaces for deployment adapters."""

from typing import Any, Protocol

from .runtime import EndpointRuntime


class TransportAdapter(Protocol):
    async def deliver_plan(self, target_id: str, envelope: bytes) -> dict[str, Any]: ...
    async def status(self, execution_id: str, principal: str) -> dict[str, Any]: ...
    async def results(self, execution_id: str, principal: str) -> dict[str, Any]: ...
    async def request_cancel(self, execution_id: str, principal: str) -> bool: ...


class FakeTransport:
    """Loopback adapter for deterministic tests and examples."""

    def __init__(self, endpoint: EndpointRuntime) -> None:
        self.endpoint = endpoint

    async def deliver_plan(self, target_id: str, envelope: bytes) -> dict[str, Any]:
        return await self.endpoint.deliver(envelope)

    async def status(self, execution_id: str, principal: str) -> dict[str, Any]:
        return self.endpoint.journal.status(execution_id, principal)

    async def results(self, execution_id: str, principal: str) -> dict[str, Any]:
        return self.endpoint.results(execution_id, principal)

    async def request_cancel(self, execution_id: str, principal: str) -> bool:
        return self.endpoint.request_cancel(execution_id, principal)
