"""Opt-in local model evaluation over the real MCP protocol."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from remote_intent.errors import IntentError

_SETTINGS = ("RIR_LLM_BASE_URL", "RIR_LLM_MODEL", "RIR_LLM_API_KEY")


def configuration(enabled: bool) -> tuple[str, str, str]:
    if not enabled or os.environ.get("RIR_ENABLE_LIVE_LLM") != "1":
        raise IntentError("EVAL_CONFIGURATION_MISSING")
    url, model, key = (os.environ.get(name, "") for name in _SETTINGS)
    if not all((url, model, key)):
        raise IntentError("EVAL_CONFIGURATION_MISSING")
    return url, model, key


async def run_fixture(
    url: str, model: str, key: str, seed: int, access_name: str, turns: int
) -> dict[str, Any]:
    from fastmcp import Client
    from openai import AsyncOpenAI
    from openai.types.chat import (
        ChatCompletionMessageFunctionToolCallParam,
        ChatCompletionMessageParam,
        ChatCompletionToolParam,
    )

    from remote_intent.mcp import SKILL_URI
    from remote_intent.policy import Access

    from .fixture import TARGET, create_fixture

    access = Access(access_name)
    trace: list[dict[str, str]] = []
    final_status = None
    with TemporaryDirectory(prefix="rir-evaluation-") as temporary:
        server, invocations = create_fixture(
            Path(temporary) / "evaluation.sqlite", seed, access
        )
        async with (
            Client(server) as mcp,
            AsyncOpenAI(base_url=url, api_key=key, timeout=90, max_retries=0) as llm,
        ):
            resource = await mcp.read_resource(SKILL_URI)
            content = getattr(resource[0], "text", "")
            tools: list[ChatCompletionToolParam] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema,
                    },
                }
                for tool in await mcp.list_tools()
            ]
            allowed = {
                tool["function"]["name"] for tool in tools if tool["type"] == "function"
            }
            messages: list[ChatCompletionMessageParam] = [
                {"role": "system", "content": content},
                {
                    "role": "user",
                    "content": f"On {TARGET}, inspect then summarize using two registered actions. "
                    "Use a valid number as input and pass the first output into the second action. "
                    "Select the final output, submit, then check status and results. Stop on denial or pending approval.",
                },
            ]
            for turn in range(turns):
                response = await llm.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice="required" if turn == 0 else "auto",
                )
                message = response.choices[0].message
                calls = message.tool_calls
                if not calls:
                    if turn == 0:
                        raise IntentError("MODEL_TOOL_CALLING_UNSUPPORTED")
                    break
                assistant_calls: list[ChatCompletionMessageFunctionToolCallParam] = []
                pending = []
                for call in calls:
                    if call.type != "function" or call.function.name not in allowed:
                        raise IntentError("MODEL_TOOL_CALLING_UNSUPPORTED")
                    arguments = json.loads(call.function.arguments)
                    if not isinstance(arguments, dict):
                        raise IntentError("MODEL_TOOL_CALLING_UNSUPPORTED")
                    assistant_calls.append(
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                    )
                    pending.append((call.id, call.function.name, arguments))
                messages.append({"role": "assistant", "tool_calls": assistant_calls})
                for identifier, name, arguments in pending:
                    result = await mcp.call_tool(name, arguments, raise_on_error=False)
                    value = result.structured_content or {}
                    code = value.get("error", {}).get("code", "")
                    trace.append({"tool": name, "code": code})
                    if name == "get_execution_status" and value.get("status") == "ok":
                        final_status = value.get("data", {}).get("status")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": identifier,
                            "content": json.dumps(value),
                        }
                    )
            else:
                raise IntentError("EVAL_TURN_LIMIT")
        called = {entry["tool"] for entry in trace}
        baseline = {
            "list_actions",
            "describe_action",
            "validate_workflow",
            "preview_execution",
            "submit_workflow",
        }
        if access == Access.ALLOW:
            passed = (
                baseline | {"get_execution_status", "get_execution_results"} <= called
                and final_status == "succeeded"
                and len(invocations) == 2
            )
        else:
            expected = (
                "SECURITY_DENIED" if access == Access.DENY else "APPROVAL_REQUIRED"
            )
            passed = not invocations and any(
                entry["code"] == expected for entry in trace
            )
        return {
            "seed": seed,
            "case": access.value,
            "trace": trace,
            "invocations": len(invocations),
            "passed": passed,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-llm", action="store_true")
    parser.add_argument(
        "--suite", type=Path, default=Path("evals/agent_skill/scenarios.yaml")
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--report-dir", type=Path, default=Path("artifacts/llm-eval"))
    args = parser.parse_args()
    try:
        url, model, key = configuration(args.live_llm)
        from remote_intent.parser import parse_yaml

        settings = parse_yaml(args.suite.read_text(encoding="utf-8"))["config"]
        start = int(settings["randomness_seed"])
        turns = min(12, int(settings["max_turns"]))
        if not 1 <= args.repetitions <= 10:
            raise IntentError("EVAL_CONFIGURATION_MISSING")
        results = [
            asyncio.run(run_fixture(url, model, key, start + repeat, access, turns))
            for repeat in range(args.repetitions)
            for access in ("allow", "deny", "requires_approval")
        ]
        args.report_dir.mkdir(parents=True, exist_ok=True)
        (args.report_dir / "summary.json").write_text(
            json.dumps(results, indent=2), encoding="utf-8"
        )
        return 0 if all(result["passed"] for result in results) else 1
    except IntentError as exc:
        print(json.dumps(exc.envelope()))
        return 1
    except Exception:
        print(json.dumps(IntentError("EVAL_MODEL_INCOMPATIBLE").envelope()))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
