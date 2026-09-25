# Native Codex routing

The installed `routing.json` records `backend`, `worker_model`,
`worker_provider`, `worker_effort`, and the selected profile. A binding without
`backend` is a legacy Router installation. Read it before delegation and run the
installed static doctor with the same CODEX_HOME/profile as the session. Neither
the binding nor doctor proves account access or the serving model.

## Router backend

Without an existing binding or explicit native selection, installation uses
`deepseek/deepseek-v4.1-flash`. `--worker-route` selects another reviewed
DeepSeek V4.1 Flash provider route. Codex chooses the child route; Codex Router
forwards it to its provider. The installer requires the exact route in the
configured local catalog with `multi_agent_version: "v2"` and checks the
recognized loopback Router URL. Provider credentials are handled by the user's
private Router setup. These checks remain Router-specific.

## Native OpenAI backend

`--worker-model MODEL` selects an exact native model identifier found in the
configured `model_catalog_json`, local `models_cache.json`, or an explicitly
supplied `--model-catalog PATH` when neither exists. Relative export paths resolve from the invoking working
directory; an installed exported source is retained on optionless updates even
if a cache appears later. The entry must advertise
`multi_agent_version: "v2"` and supported reasoning levels. The catalog default
effort is used unless `--worker-effort EFFORT` selects a listed level. Missing
metadata means capability cannot be verified locally; a known non-v2 value or
unsupported effort is incompatible. A cache or exported bundled catalog is a
capability snapshot, not proof of current account access or live availability.
The installer makes no refresh or inference request.

The installed role declares the intended `model_provider = "openai"`, model and
effort. Codex 0.157.0 applies the role's model and effort but inherits its
provider from the parent session. Therefore native preflight requires an already
active built-in OpenAI parent provider and refuses custom endpoint settings or a
redefined OpenAI provider. It never edits the root model, provider, authentication
or permissions. Project, UI and managed overrides still need inspection in the
actual session. This client limitation is documented in the
[version-pinned role source](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/core/src/agent/role.rs)
and [Codex subagent configuration](https://learn.chatgpt.com/docs/agent-configuration/subagents).

## Shared workflow and evidence

The named `astra_flash_builder` role pins its model and effort without changing
global `[agents].default_subagent_model` or reasoning defaults. The child
inherits host sandbox and approvals. Its instructions forbid recursive
delegation; `[agents].enabled = false` is emitted for compatible clients, but
Codex 0.157.0 does not apply that field from a role override. Follow the host's
actual native agent behavior and keep one worker writer by default.

Keep three evidence levels distinct:

1. The binding and role express intended configuration.
2. Client-recorded child metadata can show the selected model.
3. Service or Router request metadata can show the serving model and provider.

A worker's self-description and a successful task cannot establish serving
identity. Installation and doctor perform no inference. The optional
`--check-local-router` makes only a loopback `/models` GET in Router mode.
Check actual metadata during the first separately authorized useful task. If it
is unavailable, report runtime routing as unverified.

Delegation sends selected task context to the active provider. Preserve the
user's data-sharing restrictions, inherited sandbox and approval boundaries.
