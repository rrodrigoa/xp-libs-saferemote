# Local LLM MCP Evaluation — Operator Runbook

This runbook deliberately includes **no endpoint value**. The user selects any locally hosted OpenAI-compatible chat-completions API, including GPT-OSS 120B deployments that implement structured function/tool calling. The test client does not assume the model family, serving framework or listening port.

## Setup

1. Install the repository in a disposable local Python environment with developer/test dependencies and the optional LLM-evaluation extra.
2. Start your model server independently using your own preferred server configuration. Confirm it advertises a supported model ID and implements OpenAI-compatible function/tool calls.
3. From a local PowerShell session, enter settings **at runtime** without adding them to files:

```powershell
$env:RIR_LLM_BASE_URL = Read-Host "OpenAI-compatible API base URL"
$env:RIR_LLM_MODEL = Read-Host "Served model identifier"
$key = Read-Host "API key (or placeholder if required)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($key)
try { $env:RIR_LLM_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
$env:RIR_ENABLE_LIVE_LLM = "1"

python -m pytest tests/agent_live -m live_llm --live-llm -v

Remove-Item Env:RIR_LLM_BASE_URL, Env:RIR_LLM_MODEL, Env:RIR_LLM_API_KEY, Env:RIR_ENABLE_LIVE_LLM -ErrorAction SilentlyContinue
```

The CLI writes a sanitized summary of tool names, error codes and invocation counts.

```powershell
python -m remote_intent_evals.run --live-llm --suite evals/agent_skill/scenarios.yaml --repetitions 3 --report-dir artifacts/llm-eval
```

The current evaluator exercises one two-step observation, a denied request and a request awaiting approval. The scenario catalog also describes planned cases.

## Safety, privacy and requirements

- Never commit a `.env` containing real connection values. `.gitignore` the local eval artifact directory and any optional developer-local files.
- Do not add the model address or API key to README, example YAML, GitHub Actions, CI secrets, package metadata, fixtures, committed scripts, artifacts or logs.
- The evaluator uses a fake endpoint, fake signed plans and registered test actions. It must not require access to your real remote transport or administrator credentials.
- The harness bridges OpenAI-compatible tool calls to MCP; the model backend itself does not need native MCP support, but **must support structured function/tool calling**.
- Run the safety suite first, then the full suite. A nonzero endpoint side-effect counter for an unauthorized action is a release-blocking problem in the SDK, regardless of the model's text.
- GitHub CI never needs your locally hosted model; all merge gates use scripted agent transcripts and the actual MCP server protocol.
- The first turn requires a structured function call. The evaluator scores the fake endpoint journal and invocation counts.
