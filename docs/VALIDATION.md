# Validation evidence

Native OpenAI branch checked September 25, 2026 on Linux with Python 3.12.3.
The offline suite passed 90 tests. New synthetic cases cover native selection,
generated role and binding, config-free installation, cache/export metadata,
provider guards, unsupported capability/effort, profile preservation, backend
switch and undo, doctor drift, stale policy, read-only symlink inputs, an absent-config race,
and instrumentation that rejects credential-file reads, network calls and
subprocess calls during native inspection and installation. DeepSeek
route tests remain in the same suite. No installation into a real Codex home,
authentication read, or inference request was performed.

The earlier DeepSeek candidate was checked September 20, 2026 on macOS with
Python 3.14.3. Its evidence below is historical and applies to that candidate.

## Verified

- 63 offline tests passed. Coverage includes installation dry runs, idempotence,
  original configuration preservation, scoped policy handling,
  profile/collision/symlink checks, URL validation, fake-secret redaction,
  generated role TOML, rollback, guarded undo, plan validation and release-file
  filtering.
- Every documented V4.1 Flash route is accepted only when explicitly selected or
  preserved from an existing valid package binding and advertised as
  `multi_agent_version: "v2"`. Tests cover OpenRouter role generation, remembered
  update behavior, unreviewed-route rejection, uncertified-route rejection and
  refusal to fall back from direct DeepSeek to an available alternate provider.
- Native `/v1` and capability-path Router configurations are accepted; non-loopback hosts and unsupported URL shapes are rejected.
- Existing backup-directory permissions are preserved.
- Backup files and caches are excluded from skill installation. Release tests also cover private artifact exclusion, symlink rejection and inventory changes.

All tests use synthetic configuration, temporary directories and a local HTTP fixture. They do not require a provider account or invoke model inference. Python 3.11 is the minimum supported syntax/runtime target, but this release's local suite was run on 3.14.3; other versions and operating systems have not been tested here.

## Prior local installation evidence

The preceding package revision was installed in a macOS Codex setup using an Astra root and the exact Flash worker route. Static configuration checks passed; root configuration and authentication bytes were preserved. Its optional unauthenticated local catalog GET was rejected. The public revision's installer is verified with synthetic homes; this report does not claim it was reapplied to that real installation.

## Still unverified

Actual delegated inference through the newly supported alternate providers or native OpenAI,
native role loading for those routes in a fresh session, provider request
attribution, long-running build quality and cost savings remain unverified for
this candidate. A static report, a model catalog entry, or a worker naming itself
cannot establish these facts.

Validate real routing during the first authorized useful task, using host/router request metadata. Do not run an extra paid test as part of installation, and do not publish raw private logs or local configuration as evidence.
