# Install using Codex

Give Codex the location of this repository and the prompt below. This
authorization covers only installation, not a real delegated task.

```text
Install Astra Flash Orchestrator from this repository. Use the existing
DeepSeek route by default. If I explicitly specify a native OpenAI model,
preview and apply with --worker-model MODEL (and --worker-effort EFFORT if I
specify one). If I specify another documented DeepSeek provider, use
--worker-route ROUTE. Do not silently switch the installed backend on update.

Read README.md, install.py, POLICY.md and WORKER-INSTRUCTIONS.md first.
Inspect relevant local configuration without printing secrets, authentication
contents, full private Router URLs, or unrelated instructions. Preserve my
current root model and effort, config.toml, authentication, provider, permissions,
unrelated agents/defaults, and instructions. Do not install Router, another
runtime or dependencies, restart services, or quit Codex. Do not change the
global default subagent model or effort.

Use Python 3.11+ for every package command. Verify native custom-role/subagent
support and the exact selected model in local capability metadata. For DeepSeek,
keep Router's existing loopback and v2 catalog checks. For native OpenAI, require
an already compatible built-in OpenAI parent provider/profile; do not edit
config.toml to make it compatible. If local metadata is unavailable, explain how
I can supply an offline bundled catalog through --model-catalog. Do not read
credentials or refresh a model cache to make preflight pass.

Run offline tests and preview install.py. If they pass and the destinations
match this package's documented scope, apply with install.py --apply, adding
--replace only for a reviewed update or backend switch. I authorize installation
of the personal skill, astra_flash_builder role, and scoped managed AGENTS block.
Keep repository and managed policy restrictions. Use --profile and location
overrides consistently if applicable.

The role must pin the selected worker model/effort, inherit host
sandbox/approvals, and forbid nested delegation. Use native subagents, not an
external worker CLI. Run the installed static doctor after installation.
Verify config/auth files and existing permissions remain unchanged and unrelated
policy content is preserved. Report installed paths, root/worker settings,
tests, undo receipt and runtime limitations.

Do not launch a worker, run model inference, certify a route, commit, push or
deploy during installation. Explain that I should fully quit/reopen the host app
and start a root session invoking $astra-flash-orchestrator. Serving model
verification belongs to a separately authorized useful task using client and
service/router metadata; a model name in a file is only intended configuration.
```
