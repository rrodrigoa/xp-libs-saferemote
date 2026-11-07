"""Registration of trusted, developer-owned Python actions."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel, Field

from .contracts import Contract, Digest, Seconds, digest
from .errors import IntentError

_KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_VERSION = re.compile(r"([1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_P = ParamSpec("_P")
_R = TypeVar("_R")


class Effect(StrEnum):
    OBSERVE = "observe"
    MUTATE = "mutate"


class RetrySemantics(StrEnum):
    NEVER = "never"
    IDEMPOTENT = "idempotent"


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Trusted context handed to the worker at invocation."""

    execution_id: str
    step_id: str
    target_id: str
    cancellation: Event
    services: Any = None
    artifacts: Any = None


class ActionSpec(Contract):
    """Published action metadata derived from the registration."""

    key: str
    uses: str
    version: str
    description: str
    effect: Effect
    privilege: str | None
    retry: RetrySemantics
    timeout_seconds: Seconds
    platforms: tuple[str, ...]
    implementation_digest: Digest
    schema_digest: Digest
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    examples: list[dict[str, Any]] = Field(default_factory=list)


Invoker = Callable[[ActionContext, Any], BaseModel | Awaitable[BaseModel]]


@dataclass(frozen=True, slots=True)
class RegisteredAction:
    invoke: Invoker
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    _spec: bytes

    @property
    def spec(self) -> ActionSpec:
        return ActionSpec.model_validate_json(self._spec)


class ActionRegistry:
    """Trusted registry. The deployer supplies a verified pack content digest."""

    def __init__(self) -> None:
        self._actions: dict[str, RegisteredAction] = {}

    def register(
        self, fn: Invoker, *, implementation_digest: str, **metadata: Any
    ) -> RegisteredAction:
        """Register an installed callable with trusted deployment provenance."""
        key = metadata["key"]
        version = metadata["version"]
        match = _VERSION.fullmatch(version)
        if not match or len(version) > 24 or len(key) > 96 or not _KEY.fullmatch(key):
            raise IntentError("ACTION_VERSION_UNAVAILABLE", action=key, version=version)
        uses = f"{key}@{match[1]}"
        if uses in self._actions:
            raise IntentError("ACTION_KEY_COLLISION", action=uses)
        input_model = metadata.pop("input_model")
        output_model = metadata.pop("output_model")
        schemas = {}
        for name, model in (
            ("input_schema", input_model),
            ("output_schema", output_model),
        ):
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                raise IntentError("ACTION_SCHEMA_MISMATCH")
            schema = model.model_json_schema(
                mode="validation" if name == "input_schema" else "serialization"
            )
            for candidate in (schema, *schema.get("$defs", {}).values()):
                if (
                    candidate.get("properties") is not None
                    and candidate.get("additionalProperties") is not False
                ):
                    raise IntentError("ACTION_SCHEMA_MISMATCH")
            if (
                schema.get("type") != "object"
                or model.model_config.get("strict") is not True
            ):
                raise IntentError("ACTION_SCHEMA_MISMATCH")
            schemas[name] = schema
        options = dict(
            privilege=None, retry=RetrySemantics.NEVER, timeout_seconds=30, platforms=()
        )
        options.update(metadata)
        spec = ActionSpec.model_validate(
            dict(
                **options,
                uses=uses,
                implementation_digest=implementation_digest,
                schema_digest=digest(schemas),
                **schemas,
            )
        )
        registered = RegisteredAction(
            fn, input_model, output_model, spec.model_dump_json().encode()
        )
        self._actions[uses] = registered
        return registered

    def install(self, fn: Invoker, *, implementation_digest: str) -> RegisteredAction:
        """Install a callable decorated by a trusted publisher."""
        return self.register(
            fn,
            implementation_digest=implementation_digest,
            **getattr(fn, "__remote_intent_registration__", {}),
        )

    def resolve(self, uses: str, platform: str) -> RegisteredAction:
        """Resolve an exact advertised major for a trusted platform."""
        registered = self._actions.get(uses)
        if registered is None:
            raise IntentError("ACTION_UNKNOWN", action=uses)
        spec = registered.spec
        if spec.platforms and platform not in spec.platforms:
            raise IntentError("ACTION_TARGET_UNSUPPORTED", action=uses)
        return registered

    def catalog(self, platform: str) -> list[ActionSpec]:
        """Generate the current target-compatible catalog."""
        specs = [entry.spec for _, entry in sorted(self._actions.items())]
        return [
            spec for spec in specs if not spec.platforms or platform in spec.platforms
        ]


def action(**metadata: Any) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Declare registration metadata without implicitly installing code."""

    def decorate(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        fn.__dict__["__remote_intent_registration__"] = dict(metadata)
        return fn

    return decorate
