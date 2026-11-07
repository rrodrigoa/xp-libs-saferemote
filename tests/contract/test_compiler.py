import json

import pytest

from remote_intent import IntentError
from tests.agent_sim.test_protocol import document, fixture


@pytest.mark.parametrize(
    ("variant", "code"),
    [
        ("version", "WORKFLOW_VERSION_UNSUPPORTED"),
        ("principal", "WORKFLOW_SCHEMA_INVALID"),
        ("duplicate", "WORKFLOW_SCHEMA_INVALID"),
        ("unknown", "ACTION_UNKNOWN"),
        ("cycle", "DAG_CYCLE"),
        ("missing", "REFERENCE_UNAVAILABLE"),
        ("bad-condition", "CONDITION_INVALID"),
        ("skip-hazard", "CONDITION_SOURCE_SKIPPED"),
        ("bad-after", "WORKFLOW_SCHEMA_INVALID"),
        ("extra", "ACTION_INPUT_INVALID"),
        ("required", "ACTION_INPUT_INVALID"),
        ("stale", "CATALOG_STALE"),
    ],
)
def test_compile_rejections_are_side_effect_free(tmp_path, variant, code):
    _, controller, _, calls = fixture(tmp_path / "run.sqlite", 23)
    catalog = controller.catalog("fixture-user", "fake-endpoint")
    source = document(*catalog["actions"])
    first, second = source["spec"]["steps"]
    epoch = catalog["catalog_epoch"]
    if variant == "version":
        source["apiVersion"] = "old"
    elif variant == "principal":
        source["principal"] = "operator"
    elif variant == "duplicate":
        second["id"] = first["id"]
    elif variant == "unknown":
        first["uses"] = "absent.action@1"
    elif variant == "cycle":
        first["depends_on"] = [second["id"]]
        next(iter(second["with"].values()))["fallback"] = 1
    elif variant == "missing":
        next(iter(second["with"].values()))["ref"] = "steps.measure.outputs.absent"
    elif variant == "bad-condition":
        second["when"] = {"eval": "true"}
    elif variant == "skip-hazard":
        first["when"] = {"ref": "steps.summarize.outputs.output_23", "exists": False}
    elif variant == "bad-after":
        second["after"] = {"measure": []}
    elif variant == "extra":
        first["with"]["command"] = "text"
    elif variant == "required":
        first["with"].clear()
    elif variant == "stale":
        epoch = "outdated"
    with pytest.raises(IntentError) as error:
        controller.compile("fixture-user", json.dumps(source), epoch)
    assert error.value.code == code
    assert calls == []


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        ("dangling-dependency", "REFERENCE_UNAVAILABLE"),
        ("invalid-output-node", "REFERENCE_UNAVAILABLE"),
        ("invalid-ref-node", "REFERENCE_UNAVAILABLE"),
        ("invalid-fallback-node", "REFERENCE_UNAVAILABLE"),
        ("wrong-compare-type", "CONDITION_INVALID"),
    ],
)
def test_compile_reference_shapes(tmp_path, edit, code):
    _, controller, _, calls = fixture(tmp_path / "shapes.sqlite", 35)
    actions = controller.catalog("fixture-user", "fake-endpoint")["actions"]
    source = document(*actions)
    first, second = source["spec"]["steps"]
    ref = next(iter(second["with"].values()))
    if edit == "dangling-dependency":
        first["depends_on"] = ["absent"]
    elif edit == "invalid-output-node":
        source["spec"]["outputs"]["summary"]["executable"] = True
    elif edit == "invalid-ref-node":
        ref["other"] = 1
    elif edit == "invalid-fallback-node":
        ref["fallback"] = {"ref": ref["ref"]}
    else:
        second["when"] = {"ref": ref["ref"], "equals": "wrong"}
    with pytest.raises(IntentError) as error:
        controller.compile("fixture-user", json.dumps(source))
    assert error.value.code == code
    assert calls == []


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("nested-list", None),
        ("too-many-items", "ACTION_INPUT_INVALID"),
        ("missing-required", "ACTION_INPUT_INVALID"),
        ("extra-field", "ACTION_INPUT_INVALID"),
        ("string-instead-of-list", "TYPE_MISMATCH"),
        ("unknown-producer", "REFERENCE_UNAVAILABLE"),
        ("wrong-ref-type", "TYPE_MISMATCH"),
        ("in-not-list", "CONDITION_INVALID"),
    ],
)
def test_nested_typed_references(tmp_path, shape, expected):
    from pydantic import Field

    from remote_intent import Effect
    from remote_intent.contracts import Contract

    class Nested(Contract):
        nums: list[int] = Field(max_length=1)
        label: str

    class Input(Contract):
        nested: Nested

    class Output(Contract):
        count: int

    _, controller, _, calls = fixture(tmp_path / "nested.sqlite", 72)
    registry = controller.compiler.registry
    registry.register(
        lambda *_: Output(count=1),
        key="demo.nested",
        version="1.0.0",
        description="Test nested schema references",
        effect=Effect.OBSERVE,
        input_model=Input,
        output_model=Output,
        implementation_digest="b" * 64,
    )
    actions = controller.catalog("fixture-user", "fake-endpoint")["actions"]
    draft = document(*actions[1:])
    draft["spec"]["steps"][0] = {
        "id": "measure",
        "uses": "demo.nested@1",
        "with": {"nested": {"nums": [1], "label": "test"}},
    }
    reference = {"ref": "steps.measure.outputs.count"}
    values = {"nums": [reference], "label": "test"}
    draft["spec"]["steps"][1] = {
        "id": "summarize",
        "uses": "demo.nested@1",
        "with": {"nested": values},
    }
    draft["spec"]["outputs"] = {}
    if shape == "too-many-items":
        values["nums"].append(1)
    elif shape == "missing-required":
        values.pop("label")
    elif shape == "extra-field":
        values["unrecognized"] = reference
    elif shape == "string-instead-of-list":
        values["label"] = [reference]
    elif shape == "unknown-producer":
        reference["ref"] = "steps.unknown.outputs.count"
    elif shape == "wrong-ref-type":
        values["label"] = reference
    elif shape == "in-not-list":
        draft["spec"]["steps"][1]["when"] = {"ref": reference["ref"], "in": 1}
    if expected is None:
        compiled = controller.compile("fixture-user", json.dumps(draft))
        assert compiled.order == ["measure", "summarize"]
    else:
        with pytest.raises(IntentError) as error:
            controller.compile("fixture-user", json.dumps(draft))
        assert error.value.code == expected
    assert calls == []


def test_model_validation_enforces_constraints_beyond_json_schema(tmp_path):
    from pydantic import field_validator

    from remote_intent import Effect
    from remote_intent.contracts import Contract

    class Input(Contract):
        n: int

        @field_validator("n")
        @classmethod
        def even(cls, value):
            if value % 2:
                raise ValueError("internal assertion")
            return value

    _, controller, _, calls = fixture(tmp_path / "validator.sqlite", 74)
    controller.compiler.registry.register(
        lambda *_: Input(n=2),
        key="demo.even",
        version="1.0.0",
        description="Test model constraint",
        effect=Effect.OBSERVE,
        input_model=Input,
        output_model=Input,
        implementation_digest="b" * 64,
    )
    actions = controller.catalog("fixture-user", "fake-endpoint")["actions"]
    draft = document(*actions[1:])
    draft["spec"]["steps"] = [{"id": "only", "uses": "demo.even@1", "with": {"n": 1}}]
    draft["spec"]["outputs"] = {}
    with pytest.raises(IntentError) as error:
        controller.compile("fixture-user", json.dumps(draft))
    assert error.value.code == "ACTION_INPUT_INVALID"
    assert error.value.path == "spec.steps[0].with.n"
    assert "internal assertion" not in str(error.value)
    assert calls == []


def test_negative_example_cannot_resolve_unregistered_code():
    from pathlib import Path

    from remote_intent import ActionRegistry, WorkflowCompiler

    source = Path("examples/forbidden_workflow.yaml").read_text(encoding="utf-8")
    with pytest.raises(IntentError) as error:
        WorkflowCompiler(ActionRegistry(), "fake").compile(source)
    assert error.value.code == "ACTION_UNKNOWN"
