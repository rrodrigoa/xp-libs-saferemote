# XP Libs SafeRemote

Compile declarative YAML into signed plans for registered Python callables. The
`xp-libs-saferemote` distribution exposes the `remote_intent` SDK. Its core
works independently of MCP and language models.

The current implementation supports signed read-only execution through a typed
transport adapter. It denies mutating and privileged operations at endpoint
admission. The loopback transport and in-process worker are intended for trusted
local demonstrations and tests.

## Run the example

From the repository root, with Python 3.11 or later:

```console
python -m pip install .
python -m examples.demo
```

The example registers two consumer actions with strict Pydantic models. One
returns an execution-scoped artifact handle, and the other reads it and returns
a summary. See [the registration](examples/sample_actions.py) and
[the workflow](examples/summarize.yaml).

## Integrate the SDK

1. Register trusted Python functions with `@action` and `ActionRegistry.install`.
   Supply a content digest from your verified deployment process.
2. Configure an inventory target, controller signing key, endpoint trust store
   and independent policy adapters. With default policy, execution is denied.
3. Use `WorkflowCompiler` to validate and inspect a workflow. Use `Controller`
   for admission and `EndpointRuntime` to verify deliveries and execute through
   a trusted worker. Implement `TransportAdapter` around your delivery service.
4. Install the `mcp` extra and use `remote_intent.mcp.create_server` with the
   host's authentication callback. Agents discover actions and read their
   current schemas before constructing YAML.

The SDK validates plan signatures, target, validity window, catalog fingerprint,
local action identity and policy before invoking a callable. The endpoint stores
replay state in SQLite. On restart, stop all workers before quarantining
interrupted runs with `Journal.recover_exclusive()`. Inline result export
requires an explicit projection function.

See the [workflow schema](docs/generated/workflow.schema.json),
[error index](docs/generated/ERROR_CODES.md), [agent skill](skills/remote-intent/SKILL.md),
[security policy](SECURITY.md) and [release setup](docs/GITHUB_SETUP.md).

## Develop

```console
python -m pip install -c requirements-lock.txt -e ".[dev,mcp,llm-eval]"
ruff check .
ruff format --check .
mypy src examples
python -m pytest
```

The [local model runbook](docs/LOCAL_LLM_RUNBOOK.md) describes the optional
fake-endpoint evaluation.
