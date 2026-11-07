"""Generate stable workflow JSON Schema and canonical error code index."""

import argparse
import json
import re
from pathlib import Path
from string import Formatter

from remote_intent import Workflow

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "src" / "remote_intent" / "errors" / "catalog.json"


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate key {key}")
        result[key] = value
    return result


def generate():
    catalog = json.loads(CATALOG.read_bytes(), object_pairs_hook=unique)
    index = [
        "# Error codes",
        "",
        "The MCP get_error_info tool returns the canonical repair text.",
        "",
    ]
    enum = [
        '"""Generated from errors/catalog.json."""',
        "",
        "from enum import StrEnum",
        "",
        "",
        "class ErrorCode(StrEnum):",
        '    """Stable public error identifiers."""',
        "",
    ]
    for code, record in sorted(catalog.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", code):
            raise ValueError(code)
        if set(record) != {
            "message",
            "category",
            "severity",
            "retryable",
            "remediation",
            "example",
        }:
            raise ValueError(code)
        if any(
            not isinstance(record[key], str) or not record[key]
            for key in record.keys() - {"retryable"}
        ):
            raise ValueError(code)
        if type(record["retryable"]) is not bool:
            raise ValueError(code)
        placeholders = {
            key for _, key, _, _ in Formatter().parse(record["message"]) if key
        }
        if placeholders - {
            "action",
            "code",
            "expected",
            "path",
            "reason",
            "reference",
            "resource",
            "step",
            "tool",
            "version",
        }:
            raise ValueError(code)
        index.append(f"- `{code}` ({record['category']})")
        enum.append(f'    {code} = "{code}"')
    return {
        "docs/generated/ERROR_CODES.md": "\n".join(index) + "\n",
        "docs/generated/workflow.schema.json": json.dumps(
            Workflow.model_json_schema(), indent=2, sort_keys=True
        )
        + "\n",
        "src/remote_intent/errors/codes.py": "\n".join(enum) + "\n",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, content in generate().items():
        path = ROOT / name
        if args.check:
            if path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"Outdated generated file {name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
