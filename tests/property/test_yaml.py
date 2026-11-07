import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from remote_intent.errors import IntentError
from remote_intent.parser import parse_yaml


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("a: &ref [1]\nb: *ref", "YAML_UNSAFE_CONSTRUCT"),
        ("a: !!python/object/apply:os.system [whoami]", "YAML_UNSAFE_CONSTRUCT"),
        ("a: 1\na: 2", "YAML_UNSAFE_CONSTRUCT"),
        ("a: 1\n---\nb: 2", "YAML_UNSAFE_CONSTRUCT"),
        ("<<: {a: 1}", "YAML_UNSAFE_CONSTRUCT"),
        ("3: a", "YAML_UNSAFE_CONSTRUCT"),
        ("a: .nan", "YAML_UNSAFE_CONSTRUCT"),
        ("a: 1e999", "YAML_RESOURCE_LIMIT"),
        ("a: 9007199254740992", "YAML_RESOURCE_LIMIT"),
        ("a: " + "x" * 65536, "YAML_RESOURCE_LIMIT"),
        ("a: " + "x" * 8193, "YAML_RESOURCE_LIMIT"),
        ("a: " + "[" * 25 + "]" * 25, "YAML_RESOURCE_LIMIT"),
        ("a: [", "YAML_PARSE_FAILED"),
        ("[1, 2]", "WORKFLOW_SCHEMA_INVALID"),
        ("a: 1\n\u00e9: 2\ne\u0301: 3", "YAML_UNSAFE_CONSTRUCT"),
    ],
)
def test_reject_unsafe_yaml(source, code):
    with pytest.raises(IntentError) as error:
        parse_yaml(source)
    assert error.value.code == code


@given(
    st.dictionaries(
        st.from_regex(r"[a-z]{1,8}", fullmatch=True),
        st.one_of(st.none(), st.booleans(), st.integers(-1000, 1000)),
    )
)
def test_json_mapping_round_trip(value):
    assert parse_yaml(json.dumps(value)) == value


def test_json_scalar_profile_and_parser_event_budget():
    assert parse_yaml("empty:\nfloat: 1.2\ndate: 2026-01-01\nflag: yes") == {
        "empty": None,
        "float": 1.2,
        "date": "2026-01-01",
        "flag": "yes",
    }
    for source in ("a: " + "9" * 33, "a: [" + ",".join("0" for _ in range(8200)) + "]"):
        with pytest.raises(IntentError) as error:
            parse_yaml(source)
        assert error.value.code == "YAML_RESOURCE_LIMIT"
