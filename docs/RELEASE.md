# GitHub release preparation

Suggested repository name: **astra-flash-orchestrator**

Suggested GitHub description:

> Astra plans and reviews; the selected DeepSeek or native OpenAI worker builds. A native Codex workflow with verification and reversible setup.

Suggested topics: `codex`, `codex-skills`, `deepseek`, `ai-agents`, `developer-tools`, `agent-orchestration`.

## Before publishing

1. Confirm the distribution license with the maintainer and include LICENSE.
2. Run the offline tests and release integrity check. Review the selected release files for secrets, local paths and private instructions.
3. Review README claims against `docs/BENCHMARK.md`. Keep measured results scoped
   to the documented field run and do not present projections as guaranteed savings.
4. Create the public repository under the intended GitHub account. Commit and push only with explicit authorization.
5. Enable private vulnerability reporting in repository settings. No contact address is invented by this package.
6. Create a versioned release with changelog notes and the generated ZIP. Preserve the ZIP checksum printed by the release script.

Build an archive with `python3 -B scripts/release.py --zip`. The command refuses packaging if the inventory is stale. Artifacts go in ignored `dist/`. It does not commit, create a GitHub repository, upload files or publish a release.

## Installation evidence

A release should distinguish offline tests, static configuration checks, local endpoint checks and actual delegated task evidence. Never turn test totals or a successful catalog response into a claim that real model routing has been proven.
