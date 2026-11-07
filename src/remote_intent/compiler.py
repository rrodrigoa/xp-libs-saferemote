"""Compile bounded workflows without invoking registered callables."""

import re
from graphlib import CycleError, TopologicalSorter
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from .actions import ActionRegistry, ActionSpec, Effect
from .contracts import Contract, Workflow, canonical, digest
from .errors import IntentError
from .parser import parse_yaml

_REF = re.compile(r"steps\.([A-Za-z0-9_-]{1,64})\.outputs\.([A-Za-z0-9_-]{1,64})")
_ANNOTATIONS = {"title", "description", "examples", "default", "$defs"}


class CompiledPlan(Contract):
    """Resolved plan with an exact catalog fingerprint and execution order."""

    workflow: Workflow
    catalog_epoch: str
    actions: dict[str, ActionSpec]
    dependencies: dict[str, list[str]]
    order: list[str]

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json", by_alias=True))


def error_path(base: str, parts: Any) -> str:
    for part in parts:
        base += (
            f"[{part}]" if isinstance(part, int) else f".{part}" if base else str(part)
        )
    return base or "workflow"


def _expand(
    schema: dict[str, Any], root: dict[str, Any], depth: int = 0
) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref is None:
        return schema
    if (
        depth >= 24
        or not ref.startswith("#/$defs/")
        or ref[8:] not in root.get("$defs", {})
    ):
        raise IntentError("ACTION_SCHEMA_MISMATCH")
    expanded = _expand(root["$defs"][ref[8:]], root, depth + 1)
    return {
        **expanded,
        **{key: value for key, value in schema.items() if key != "$ref"},
    }


def _shape(schema: Any, root: dict[str, Any], depth: int = 0) -> Any:
    if depth >= 24:
        raise IntentError("TYPE_MISMATCH", expected="bounded reference schema")
    if isinstance(schema, list):
        return [_shape(value, root, depth + 1) for value in schema]
    if not isinstance(schema, dict):
        return schema
    return {
        key: (
            {name: _shape(child, root, depth + 1) for name, child in value.items()}
            if key == "properties"
            else _shape(value, root, depth + 1)
            if key
            in {
                "items",
                "anyOf",
                "oneOf",
                "allOf",
                "additionalProperties",
                "prefixItems",
            }
            else value
        )
        for key, value in _expand(schema, root).items()
        if key not in _ANNOTATIONS
    }


def _has_ref(value: Any) -> bool:
    if isinstance(value, dict):
        return "ref" in value or any(_has_ref(item) for item in value.values())
    return isinstance(value, list) and any(_has_ref(item) for item in value)


def _literal(
    value: Any, schema: dict[str, Any], root: dict[str, Any], path: str, action: str
) -> None:
    validator = Draft202012Validator(
        {**schema, "$defs": root.get("$defs", {})}, registry=Registry()
    )
    try:
        error = next(validator.iter_errors(value), None)
    except Unresolvable:
        raise IntentError("ACTION_SCHEMA_MISMATCH", path=path) from None
    if error is not None:
        location = error_path(path, error.absolute_path)
        if error.validator == "required" and isinstance(error.instance, dict):
            missing = next(
                field for field in error.validator_value if field not in error.instance
            )
            location = error_path(location, [missing])
        if error.validator == "additionalProperties" and isinstance(
            error.instance, dict
        ):
            unexpected = next(
                (
                    key
                    for key in error.instance
                    if key not in error.schema.get("properties", {})
                ),
                None,
            )
            if unexpected is not None:
                location = error_path(location, [unexpected])
        raise IntentError(
            "ACTION_INPUT_INVALID",
            path=location,
            action=action,
            reason="schema constraint",
        )


class _References:
    def __init__(self, workflow: Workflow, actions: dict[str, ActionSpec]) -> None:
        self.steps = {step.id: step for step in workflow.spec.steps}
        self.actions = actions

    def source(
        self,
        node: dict[str, Any],
        path: str,
        dependencies: set[str],
        *,
        condition: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        ref = node.get("ref")
        match = _REF.fullmatch(ref) if isinstance(ref, str) else None
        if match is None or match[1] not in self.steps:
            raise IntentError(
                "REFERENCE_UNAVAILABLE", path=path, reference="output reference"
            )
        producer, field = match.groups()
        root = self.actions[producer].output_schema
        if field not in root.get("properties", {}):
            raise IntentError("REFERENCE_UNAVAILABLE", path=path, reference=match[0])
        step = self.steps[producer]
        if not condition and (
            field not in root.get("required", ())
            or step.when is not None
            or step.depends_on
            or step.after
        ):
            if "fallback" not in node:
                raise IntentError("CONDITION_SOURCE_SKIPPED", path=path)
        dependencies.add(producer)
        return root["properties"][field], root

    def input(
        self,
        value: Any,
        schema: dict[str, Any],
        root: dict[str, Any],
        path: str,
        action: str,
        dependencies: set[str],
    ) -> None:
        if not _has_ref(value):
            _literal(value, schema, root, path, action)
            return
        if isinstance(value, dict) and "ref" in value:
            if value.keys() - {"ref", "fallback"}:
                raise IntentError("REFERENCE_UNAVAILABLE", path=path)
            source, source_root = self.source(value, path, dependencies)
            if _shape(source, source_root) != _shape(schema, root):
                raise IntentError(
                    "TYPE_MISMATCH", path=path, expected="compatible output schema"
                )
            if "fallback" in value:
                if _has_ref(value["fallback"]):
                    raise IntentError("REFERENCE_UNAVAILABLE", path=path)
                _literal(value["fallback"], schema, root, path, action)
            return
        expanded = _expand(schema, root)
        if isinstance(value, dict) and expanded.get("type") == "object":
            properties = expanded.get("properties", {})
            for field in expanded.get("required", ()):
                if field not in value:
                    raise IntentError(
                        "ACTION_INPUT_INVALID",
                        path=error_path(path, [field]),
                        action=action,
                        reason="required field",
                    )
            for key, item in value.items():
                child = properties.get(key, expanded.get("additionalProperties"))
                if not isinstance(child, dict):
                    raise IntentError(
                        "ACTION_INPUT_INVALID",
                        path=error_path(path, [key]),
                        action=action,
                        reason="undeclared field",
                    )
                self.input(
                    item, child, root, error_path(path, [key]), action, dependencies
                )
        elif isinstance(value, list) and isinstance(expanded.get("items"), dict):
            if (
                not expanded.get("minItems", 0)
                <= len(value)
                <= expanded.get("maxItems", 8192)
            ):
                raise IntentError(
                    "ACTION_INPUT_INVALID",
                    path=path,
                    action=action,
                    reason="array size",
                )
            for index, item in enumerate(value):
                self.input(
                    item,
                    expanded["items"],
                    root,
                    error_path(path, [index]),
                    action,
                    dependencies,
                )
        else:
            raise IntentError(
                "TYPE_MISMATCH", path=path, expected="reference compatible container"
            )

    def condition(self, node: Any, path: str, dependencies: set[str]) -> None:
        if isinstance(node, dict):
            for operator in ("all", "any"):
                if (
                    set(node) == {operator}
                    and isinstance(node[operator], list)
                    and 1 <= len(node[operator]) <= 32
                ):
                    for index, child in enumerate(node[operator]):
                        self.condition(
                            child, f"{path}.{operator}[{index}]", dependencies
                        )
                    return
            if set(node) == {"ref", "exists"} and type(node["exists"]) is bool:
                self.source(node, path, dependencies, condition=True)
                return
            if set(node) in ({"ref", "equals"}, {"ref", "in"}):
                schema, root = self.source(node, path, dependencies, condition=True)
                values = [node["equals"]] if "equals" in node else node["in"]
                if isinstance(values, list) and 1 <= len(values) <= 32:
                    try:
                        for value in values:
                            _literal(value, schema, root, path, "condition")
                    except IntentError:
                        raise IntentError("CONDITION_INVALID", path=path) from None
                    return
        raise IntentError("CONDITION_INVALID", path=path)


class WorkflowCompiler:
    """Compile against a trusted, currently installed registry."""

    def __init__(self, registry: ActionRegistry, platform: str) -> None:
        self.registry = registry
        self.platform = platform

    def compile(self, workflow_yaml: str) -> CompiledPlan:
        source = parse_yaml(workflow_yaml)
        if "apiVersion" in source and source["apiVersion"] != "remote.intent/v1":
            raise IntentError("WORKFLOW_VERSION_UNSUPPORTED", path="apiVersion")
        try:
            workflow = Workflow.model_validate(source)
        except ValidationError as exc:
            issue = exc.errors(include_input=False)[0]
            raise IntentError(
                "WORKFLOW_SCHEMA_INVALID", path=error_path("", issue["loc"])
            ) from None
        return self.resolve(workflow)

    def resolve(self, workflow: Workflow) -> CompiledPlan:
        actions: dict[str, ActionSpec] = {}
        for index, step in enumerate(workflow.spec.steps):
            path = f"spec.steps[{index}]"
            if step.id in actions:
                raise IntentError("WORKFLOW_SCHEMA_INVALID", path=f"{path}.id")
            try:
                actions[step.id] = self.registry.resolve(step.uses, self.platform).spec
            except IntentError as exc:
                raise IntentError(
                    exc.code, path=f"{path}.uses", action=step.uses
                ) from None
        references = _References(workflow, actions)
        graph: dict[str, list[str]] = {}
        for index, step in enumerate(workflow.spec.steps):
            path = f"spec.steps[{index}]"
            action = actions[step.id]
            deps = set(step.depends_on) | step.after.keys()
            if deps - actions.keys() or set(step.depends_on) & step.after.keys():
                raise IntentError("REFERENCE_UNAVAILABLE", path=f"{path}.depends_on")
            if step.after and (
                action.effect != Effect.OBSERVE
                or any(not states for states in step.after.values())
            ):
                raise IntentError("WORKFLOW_SCHEMA_INVALID", path=f"{path}.after")
            schema = action.input_schema
            references.input(
                step.arguments, schema, schema, f"{path}.with", step.uses, deps
            )
            if not _has_ref(step.arguments):
                registered = self.registry.resolve(step.uses, self.platform)
                try:
                    registered.input_model.model_validate_json(
                        canonical(step.arguments)
                    )
                except ValidationError as exc:
                    issue = exc.errors(include_input=False)[0]
                    raise IntentError(
                        "ACTION_INPUT_INVALID",
                        path=error_path(f"{path}.with", issue["loc"]),
                        action=step.uses,
                        reason="model constraint",
                    ) from None
            if step.when is not None:
                references.condition(step.when, f"{path}.when", deps)
            graph[step.id] = sorted(deps)
        try:
            order = list(TopologicalSorter(dict(sorted(graph.items()))).static_order())
        except CycleError:
            raise IntentError("DAG_CYCLE", path="spec.steps") from None
        for name, output in workflow.spec.outputs.items():
            path = f"spec.outputs.{name}"
            if output.keys() - {"ref", "fallback"}:
                raise IntentError("REFERENCE_UNAVAILABLE", path=path)
            schema, root = references.source(output, path, set())
            if "fallback" in output:
                _literal(output["fallback"], schema, root, path, "output")
        epoch = digest(
            [
                spec.model_dump(mode="json")
                for spec in self.registry.catalog(self.platform)
            ]
        )
        return CompiledPlan(
            workflow=workflow,
            catalog_epoch=epoch,
            actions=actions,
            dependencies=graph,
            order=order,
        )
