"""Bounded YAML parser using a JSON-like scalar profile."""

import json
import math
import re
import unicodedata
from typing import Any

import yaml
from yaml.events import AliasEvent, DocumentStartEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .errors import IntentError

_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_RESERVED_FLOATS = {".nan", ".inf", "+.inf", "-.inf"}


def _scalar(node: ScalarNode, path: str) -> Any:
    value = unicodedata.normalize("NFC", node.value)
    if len(value) > 8192 or any(unicodedata.category(char) == "Cs" for char in value):
        raise IntentError("YAML_RESOURCE_LIMIT", path=path)
    if node.style is not None:
        return value
    if value in ("true", "false", "null"):
        return json.loads(value)
    if value == "":
        return None
    if _NUMBER.fullmatch(value):
        if len(value) > 32:
            raise IntentError("YAML_RESOURCE_LIMIT", path=path)
        number = json.loads(value)
        if (isinstance(number, int) and abs(number) > 2**53 - 1) or not math.isfinite(
            number
        ):
            raise IntentError("YAML_RESOURCE_LIMIT", path=path)
        return number
    if value.lower() in _RESERVED_FLOATS:
        raise IntentError("YAML_UNSAFE_CONSTRUCT", path=path)
    return value


def _convert(node: Node, path: str) -> Any:
    if isinstance(node, ScalarNode):
        return _scalar(node, path)
    if isinstance(node, SequenceNode):
        return [
            _convert(item, f"{path}[{index}]") for index, item in enumerate(node.value)
        ]
    if not isinstance(node, MappingNode):
        raise IntentError("YAML_UNSAFE_CONSTRUCT", path=path)
    result = {}
    for key_node, value_node in node.value:
        key = _convert(key_node, path)
        if not isinstance(key, str) or key == "<<" or key in result:
            raise IntentError("YAML_UNSAFE_CONSTRUCT", path=path)
        result[key] = _convert(value_node, f"{path}.{key}" if path else key)
    return result


def parse_yaml(source: str) -> dict[str, Any]:
    """Parse one YAML mapping with pre-composition structural limits."""
    try:
        if len(source.encode("utf-8")) > 65536:
            raise IntentError("YAML_RESOURCE_LIMIT")
        depth = documents = 0
        for index, event in enumerate(yaml.parse(source, Loader=yaml.BaseLoader)):
            if index > 8192:
                raise IntentError("YAML_RESOURCE_LIMIT")
            if (
                isinstance(event, AliasEvent)
                or getattr(event, "anchor", None)
                or getattr(event, "tag", None)
            ):
                raise IntentError("YAML_UNSAFE_CONSTRUCT")
            if isinstance(event, DocumentStartEvent):
                documents += 1
                if documents > 1 or event.tags or event.version:
                    raise IntentError("YAML_UNSAFE_CONSTRUCT")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > 24:
                    raise IntentError("YAML_RESOURCE_LIMIT")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        root = yaml.compose(source, Loader=yaml.BaseLoader)
    except (yaml.YAMLError, UnicodeError):
        raise IntentError("YAML_PARSE_FAILED") from None
    if not isinstance(root, MappingNode):
        raise IntentError("WORKFLOW_SCHEMA_INVALID")
    result: dict[str, Any] = _convert(root, "")
    return result
