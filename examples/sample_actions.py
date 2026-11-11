"""Example consumer pack backed by synthetic observations."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from remote_intent import ActionContext, ArtifactRef, Effect, RetrySemantics, action


class CollectLogsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    channel: Literal["System", "Application"]
    lookback_minutes: int = Field(ge=1, le=1440)


class CollectLogsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    logs: ArtifactRef
    count: int


@action(
    key="example.logs.collect",
    version="1.0.0",
    description="Collect bounded log records",
    input_model=CollectLogsInput,
    output_model=CollectLogsOutput,
    effect=Effect.OBSERVE,
    retry=RetrySemantics.IDEMPOTENT,
    timeout_seconds=60,
    examples=[{"channel": "System", "lookback_minutes": 60}],
)
async def collect_logs(ctx: ActionContext, args: CollectLogsInput) -> CollectLogsOutput:
    # Synthetic records keep the sample independent of host state.
    rows = [{"source": args.channel, "severity": "info"}] * min(
        args.lookback_minutes, 3
    )
    artifact = await ctx.artifacts.put_json(rows, sensitivity="internal")
    return CollectLogsOutput(logs=artifact, count=len(rows))


class SummarizeLogsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    logs: ArtifactRef
    group_by: list[Literal["source", "severity"]] = Field(min_length=1, max_length=2)


class SummarizeLogsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: dict[str, int]


@action(
    key="example.logs.summarize",
    version="1.0.0",
    description="Summarize a log artifact, using any developer-chosen implementation",
    input_model=SummarizeLogsInput,
    output_model=SummarizeLogsOutput,
    effect=Effect.OBSERVE,
    retry=RetrySemantics.IDEMPOTENT,
    timeout_seconds=90,
    examples=[{"logs": {"ref": "steps.collect.outputs.logs"}, "group_by": ["source"]}],
)
def summarize_logs(ctx: ActionContext, args: SummarizeLogsInput) -> SummarizeLogsOutput:
    rows = json.loads(ctx.artifacts.read(args.logs))
    summary: dict[str, int] = {}
    for row in rows:
        key = "/".join(row[field] for field in args.group_by)
        summary[key] = summary.get(key, 0) + 1
    return SummarizeLogsOutput(summary=summary)
